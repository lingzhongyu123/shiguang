import json
import os
import uuid
from datetime import datetime

import psycopg2.extras
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, db, export, llm, parser

app = FastAPI(title="拾光")

STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


# ---------- 检索 ----------

def _vec_literal(vec):
    return "[" + ",".join("%.6f" % x for x in vec) + "]"


def search_related(conn, query_vec, top_k=5, exclude_entry=None):
    """混合评分检索:0.65*语义 + 0.20*时间衰减(半年衰期) + 0.15*来源权重。

    pgvector 先按 cosine 粗召回 50 条,再在 Python 里精排。"""
    if not db.vector_ok() or query_vec is None:
        return []
    q = ("SELECT e.source_type, e.source_id, e.content, en.date,"
         " (e.embedding <=> %s::vector) AS dist FROM embeddings e "
         "LEFT JOIN entries en ON e.source_type='entry' AND en.id=e.source_id "
         "WHERE (%s IS NULL OR e.source_id != %s OR e.source_type != 'entry') "
         "ORDER BY dist LIMIT 50")
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(q, (_vec_literal(query_vec), exclude_entry, exclude_entry))
        rows = cur.fetchall()
    today = datetime.now().date()
    scored = []
    for r in rows:
        sim = max(0.0, 1.0 - float(r["dist"]))
        days = 365
        if r["date"]:
            try:
                days = abs((today - datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date()).days)
            except ValueError:
                pass
        decay = 2.718281828 ** (-days / 180.0)
        imp = 0.7 if r["source_type"] == "memory" else 0.4  # 记忆是确认过的高价值内容
        score = 0.65 * sim + 0.20 * decay + 0.15 * imp
        scored.append((score, {"source_type": r["source_type"], "source_id": r["source_id"],
                               "content": r["content"], "date": r["date"] or "",
                               "score": round(score, 4)}))
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:top_k]]


def store_embedding(conn, source_type, source_id, content, vec):
    if not db.vector_ok() or vec is None:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE source_type=%s AND source_id=%s",
                    (source_type, source_id))
        cur.execute("INSERT INTO embeddings(source_type,source_id,content,embedding)"
                    " VALUES(%s,%s,%s,%s::vector)",
                    (source_type, source_id, content, _vec_literal(vec)))


# ---------- auth ----------

class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    if not auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="密码不对")
    response.set_cookie(auth.COOKIE, auth.make_token(), max_age=auth.TTL,
                        httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


# ---------- process ----------

class ProcessBody(BaseModel):
    raw: str = ""
    mode: str = ""
    date: str = ""


def _job_update(jid, status=None, stage=None, error=None, entry_id=None):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status=COALESCE(%s,status), stage=COALESCE(%s,stage),"
                " error=COALESCE(%s,error), entry_id=COALESCE(%s,entry_id),"
                " updated_at=to_char(now() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
                " WHERE id=%s",
                (status, stage, error, entry_id, jid))


@app.post("/api/process", dependencies=[Depends(auth.require_login)])
def process_log(body: ProcessBody, tasks: BackgroundTasks):
    if not body.raw and not body.mode:
        raise HTTPException(status_code=400, detail="口述内容和模式至少填一个")
    jid = uuid.uuid4().hex[:12]
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO jobs(id,status) VALUES(%s,'queued')", (jid,))
    tasks.add_task(_run_process, jid, body)
    return {"jobId": jid}


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth.require_login)])
def get_job(jid: str):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM jobs WHERE id=%s", (jid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="任务不存在或已过期")
            out = {"id": row["id"], "status": row["status"], "stage": row["stage"],
                   "error": row["error"], "entryId": row["entry_id"]}
            if row["status"] == "done" and row["entry_id"]:
                cur.execute("SELECT * FROM entries WHERE id=%s", (row["entry_id"],))
                e = cur.fetchone()
                if e:
                    cur.execute("SELECT * FROM events WHERE source_entry=%s"
                                " ORDER BY importance DESC", (row["entry_id"],))
                    out["entry"] = db.entry_to_dict(e)
                    out["entry"]["events"] = [db.event_to_dict(r) for r in cur.fetchall()]
            return out


def _run_process(jid, body):
    try:
        entry = _process(jid, body)
        _job_update(jid, status="done", entry_id=entry["id"])
    except llm.LLMError as e:
        _job_update(jid, status="failed", error=str(e))
    except Exception as e:  # 兜底:任务异常不能丢
        _job_update(jid, status="failed", error="%s: %s" % (type(e).__name__, e))


def _process(jid, body):
    date = body.date or datetime.now().strftime("%Y-%m-%d")

    # ① embedding + Context Builder:档案 + 活跃目标 + 最近 3 条 + 语义相关 5 条
    _job_update(jid, status="running", stage="embedding")
    query_vec = None
    with db.db() as conn:
        sections = db.archive_sections(conn, for_prompt=True)["sections"]
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM goals WHERE status='active' ORDER BY id")
            goals = [db.goal_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 3")
            recent = [db.entry_to_dict(r) for r in cur.fetchall()][::-1]
        if body.raw and db.vector_ok():
            try:
                query_vec = llm.embed_texts([body.raw[:1500]])[0]
            except llm.LLMError:
                query_vec = None
        related = search_related(conn, query_vec, top_k=5)

    prompt = llm.build_user_prompt(sections, goals, recent, related,
                                   body.raw.strip(), date, body.mode)
    deep = body.mode in llm.DEEP_MODES
    _job_update(jid, stage="generating")
    reply = llm.call_glm(prompt, deep=deep)  # LLMError 由 _run_process 记入 jobs
    _job_update(jid, stage="saving")
    parsed = parser.parse_json_result(reply, body.raw, date, body.mode)

    # ② 入库:entry + 待审核记忆 + 事件,同一事务;向量入库在事务提交后单独尝试
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "INSERT INTO entries(date,mode,raw,polished,summary,advice,mempack,profile_delta,evidence,llm_reply)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (parsed["date"], parsed["mode"], parsed["raw"], parsed["polished"],
                 parsed["summary"], json.dumps(parsed["advice"], ensure_ascii=False),
                 json.dumps([m["text"] for m in parsed["memories"]], ensure_ascii=False),
                 parsed["profileDelta"], parsed["evi"], reply))
            row = cur.fetchone()
            entry_id = row["id"]
            for m in parsed["memories"]:
                cur.execute(
                    "INSERT INTO pending_memories(text,cat,src_entry,evi,confidence,note)"
                    " VALUES(%s,%s,%s,%s,%s,%s)",
                    (m["text"], m["cat"], entry_id, parsed["evi"],
                     m.get("confidence"), m.get("note", "")))
            for ev in parsed["events"]:
                cur.execute(
                    "INSERT INTO events(title,description,event_type,date,importance,source_entry)"
                    " VALUES(%s,%s,%s,%s,%s,%s)",
                    (ev["title"], ev["description"], ev["type"],
                     parsed["date"], ev["importance"], entry_id))
        conn.commit()  # 核心数据先落库;下面的向量写入失败不回滚它们
        content = parsed["polished"] or parsed["raw"]
        if content and db.vector_ok():
            vec = query_vec
            if vec is None:
                try:
                    vec = llm.embed_texts([content[:1500]])[0]
                except llm.LLMError:
                    vec = None
            try:
                store_embedding(conn, "entry", entry_id, content[:2000], vec)
                conn.commit()
            except Exception:
                conn.rollback()  # 只回滚向量部分
        out = db.entry_to_dict(row)
        out["events"] = parsed["events"]
        return out


# ---------- entries ----------

def _entries_with_events(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 500")
        entries = [db.entry_to_dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM events ORDER BY importance DESC, id")
        events = [db.event_to_dict(r) for r in cur.fetchall()]
    by_src = {}
    for ev in events:
        by_src.setdefault(ev["src"], []).append(ev)
    for e in entries:
        e["events"] = by_src.get(e["id"], [])
    return entries


@app.get("/api/entries", dependencies=[Depends(auth.require_login)])
def list_entries():
    with db.db() as conn:
        return _entries_with_events(conn)


@app.get("/api/entries/{eid}", dependencies=[Depends(auth.require_login)])
def get_entry(eid: int):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM entries WHERE id=%s", (eid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="日志不存在")
            e = db.entry_to_dict(row)
            cur.execute("SELECT * FROM events WHERE source_entry=%s ORDER BY importance DESC", (eid,))
            e["events"] = [db.event_to_dict(r) for r in cur.fetchall()]
            return e


@app.delete("/api/entries/{eid}", dependencies=[Depends(auth.require_login)])
def delete_entry(eid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM entries WHERE id=%s", (eid,))
            cur.execute("DELETE FROM embeddings WHERE source_type='entry' AND source_id=%s", (eid,))
    return {"ok": True}


# ---------- pending memories ----------

@app.get("/api/pending", dependencies=[Depends(auth.require_login)])
def list_pending():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pending_memories WHERE status='pending' ORDER BY id DESC")
            return [db.pending_to_dict(r) for r in cur.fetchall()]


class ConfirmBody(BaseModel):
    cat: str


@app.post("/api/pending/{pid}/confirm", dependencies=[Depends(auth.require_login)])
def confirm_pending(pid: int, body: ConfirmBody):
    if body.cat not in config.SECTIONS:
        raise HTTPException(status_code=400, detail="归类不合法")
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pending_memories WHERE id=%s AND status='pending'", (pid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="待审核记忆不存在")
            cur.execute("SELECT 1 FROM archive WHERE section=%s AND text=%s",
                        (body.cat, row["text"]))
            if not cur.fetchone():
                src = [row["src_entry"]] if row["src_entry"] else []
                cur.execute(
                    "INSERT INTO archive(section,text,source_entries,confidence,last_verified)"
                    " VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (body.cat, row["text"], src, row["confidence"],
                     datetime.now().strftime("%Y-%m-%d")))
                aid = cur.fetchone()["id"]
                # 记忆也进向量库,供语义检索
                if db.vector_ok():
                    try:
                        vec = llm.embed_texts([row["text"][:1000]])[0]
                        store_embedding(conn, "memory", aid, row["text"], vec)
                    except Exception:
                        conn.rollback()
            cur.execute("UPDATE pending_memories SET status='archived', cat=%s WHERE id=%s",
                        (body.cat, pid))
    return {"ok": True}


@app.post("/api/pending/{pid}/skip", dependencies=[Depends(auth.require_login)])
def skip_pending(pid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_memories SET status='discarded' WHERE id=%s", (pid,))
    return {"ok": True}


# ---------- archive lifecycle ----------

class ArchiveStatusBody(BaseModel):
    status: str  # active | deprecated


@app.post("/api/archive/{aid}/status", dependencies=[Depends(auth.require_login)])
def set_archive_status(aid: int, body: ArchiveStatusBody):
    if body.status not in ("active", "deprecated"):
        raise HTTPException(status_code=400, detail="状态不合法")
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE archive SET status=%s,"
                        " updated=to_char(now() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
                        " WHERE id=%s", (body.status, aid))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="记忆不存在")
            if body.status == "deprecated":
                cur.execute("DELETE FROM embeddings WHERE source_type='memory' AND source_id=%s", (aid,))
    return {"ok": True}


# ---------- events ----------

@app.get("/api/events", dependencies=[Depends(auth.require_login)])
def list_events():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM events ORDER BY date DESC, importance DESC LIMIT 500")
            return [db.event_to_dict(r) for r in cur.fetchall()]


@app.delete("/api/events/{eid}", dependencies=[Depends(auth.require_login)])
def delete_event(eid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id=%s", (eid,))
    return {"ok": True}


# ---------- goals ----------

_goal_vec_cache = {}


class GoalBody(BaseModel):
    title: str
    description: str = ""
    deadline: str = ""


class GoalPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    progress: int | None = None
    deadline: str | None = None


@app.get("/api/goals", dependencies=[Depends(auth.require_login)])
def list_goals():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM goals ORDER BY status='active' DESC, id DESC")
            goals = [db.goal_to_dict(r) for r in cur.fetchall()]
        # 停滞检测:每个活跃目标,检索最相关的 3 条日志,看最近一条距今几天
        today = datetime.now().date()
        for g in goals:
            g["daysIdle"] = None
            if g["status"] != "active" or not db.vector_ok():
                continue
            key = (g["id"], g["title"])
            if key not in _goal_vec_cache:
                try:
                    _goal_vec_cache[key] = llm.embed_texts([g["title"][:500]])[0]
                except llm.LLMError:
                    continue
            hits = search_related(conn, _goal_vec_cache[key], top_k=3)
            dates = []
            for h in hits:
                if h["source_type"] == "entry" and h["date"]:
                    try:
                        dates.append(datetime.strptime(h["date"][:10], "%Y-%m-%d").date())
                    except ValueError:
                        pass
            if dates:
                g["daysIdle"] = (today - max(dates)).days
    return goals


@app.post("/api/goals", dependencies=[Depends(auth.require_login)])
def add_goal(body: GoalBody):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("INSERT INTO goals(title,description,deadline) VALUES(%s,%s,%s) RETURNING *",
                        (body.title.strip(), body.description, body.deadline))
            return db.goal_to_dict(cur.fetchone())


@app.patch("/api/goals/{gid}", dependencies=[Depends(auth.require_login)])
def patch_goal(gid: int, body: GoalPatch):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM goals WHERE id=%s", (gid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="目标不存在")
            vals = {k: getattr(body, k) if getattr(body, k) is not None else row[k]
                    for k in ("title", "description", "status", "progress", "deadline")}
            if vals["status"] not in ("active", "paused", "done", "abandoned"):
                raise HTTPException(status_code=400, detail="状态不合法")
            cur.execute("UPDATE goals SET title=%s,description=%s,status=%s,progress=%s,deadline=%s"
                        " WHERE id=%s RETURNING *",
                        (vals["title"], vals["description"], vals["status"],
                         vals["progress"], vals["deadline"], gid))
            return db.goal_to_dict(cur.fetchone())


@app.delete("/api/goals/{gid}", dependencies=[Depends(auth.require_login)])
def delete_goal(gid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM goals WHERE id=%s", (gid,))
    return {"ok": True}


# ---------- todos ----------

class TodoBody(BaseModel):
    text: str
    due: str = ""
    src_entry: int | None = None


class TodoPatch(BaseModel):
    text: str | None = None
    due: str | None = None
    done: bool | None = None


@app.get("/api/todos", dependencies=[Depends(auth.require_login)])
def list_todos():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM todos ORDER BY done, id")
            return [db.todo_to_dict(r) for r in cur.fetchall()]


@app.post("/api/todos", dependencies=[Depends(auth.require_login)])
def add_todo(body: TodoBody):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("INSERT INTO todos(text,due,src_entry) VALUES(%s,%s,%s) RETURNING *",
                        (body.text.strip(), body.due, body.src_entry))
            return db.todo_to_dict(cur.fetchone())


@app.patch("/api/todos/{tid}", dependencies=[Depends(auth.require_login)])
def patch_todo(tid: int, body: TodoPatch):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM todos WHERE id=%s", (tid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="待办不存在")
            text = body.text if body.text is not None else row["text"]
            due = body.due if body.due is not None else row["due"]
            done = (1 if body.done else 0) if body.done is not None else row["done"]
            cur.execute("UPDATE todos SET text=%s,due=%s,done=%s WHERE id=%s RETURNING *",
                        (text, due, done, tid))
            return db.todo_to_dict(cur.fetchone())


@app.delete("/api/todos/{tid}", dependencies=[Depends(auth.require_login)])
def delete_todo(tid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM todos WHERE id=%s", (tid,))
    return {"ok": True}


# ---------- profile ----------

@app.get("/api/profile", dependencies=[Depends(auth.require_login)])
def get_profile():
    with db.db() as conn:
        return db.archive_sections(conn)


class ProfileBody(BaseModel):
    text: str  # "## 节名" 格式的整段文本,服务端解析


@app.put("/api/profile", dependencies=[Depends(auth.require_login)])
def put_profile(body: ProfileBody):
    sections = parser.parse_profile_textarea(body.text)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("DELETE FROM embeddings WHERE source_type='memory'")
            cur.execute("DELETE FROM archive")
            for sec, items in sections.items():
                for item in items:
                    cur.execute("INSERT INTO archive(section,text,updated) VALUES(%s,%s,%s) RETURNING id",
                                (sec, item, now))
                    aid = cur.fetchone()["id"]
                    if db.vector_ok():
                        try:
                            vec = llm.embed_texts([item[:1000]])[0]
                            store_embedding(conn, "memory", aid, item, vec)
                        except Exception:
                            conn.rollback()
    return {"ok": True}


# ---------- export / import ----------

def _export_data():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pending_memories WHERE status='pending' ORDER BY id")
            pending = [db.pending_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM todos ORDER BY id")
            todos = [db.todo_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM events ORDER BY id")
            events = [db.event_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM goals ORDER BY id")
            goals = [db.goal_to_dict(r) for r in cur.fetchall()]
            rows = _entries_with_events(conn)
        return {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "profile": db.archive_sections(conn),
            "pending": pending, "todos": todos, "events": events, "goals": goals,
            "rows": rows,
        }


@app.get("/api/export/md", dependencies=[Depends(auth.require_login)])
def export_md():
    md = export.to_markdown(_export_data())
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="shiguang-backup.md"'})


@app.get("/api/export/json", dependencies=[Depends(auth.require_login)])
def export_json():
    js = export.to_json(_export_data())
    return PlainTextResponse(
        js, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="shiguang-backup.json"'})


@app.post("/api/import", dependencies=[Depends(auth.require_login)])
async def import_backup(request: Request):
    try:
        d = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 解析失败")
    if not isinstance(d, dict) or "entries" not in d or "profile" not in d:
        raise HTTPException(status_code=400, detail="不是有效的拾光备份格式")
    n_entries = n_pending = n_todos = 0
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            for e in d.get("entries", []):
                cur.execute(
                    "INSERT INTO entries(date,mode,raw,polished,summary,advice,mempack,profile_delta,evidence)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (e.get("date", ""), e.get("mode", ""), e.get("raw", ""),
                     e.get("polished", ""), e.get("summary", ""),
                     json.dumps(e.get("advice", []), ensure_ascii=False),
                     json.dumps(e.get("memPack", e.get("mempack", [])), ensure_ascii=False),
                     e.get("profileDelta", e.get("profile_delta", "")),
                     e.get("evi", e.get("evidence", ""))))
                entry_id = cur.fetchone()["id"]
                n_entries += 1
                for m in e.get("memPack", e.get("mempack", [])):
                    line = m if isinstance(m, str) else m.get("text", "")
                    cat, line = parser.split_mem_line(line)
                    if cat is None:
                        cat = parser.guess_cat(line)
                    cur.execute(
                        "INSERT INTO pending_memories(text,cat,src_entry) VALUES(%s,%s,%s)",
                        (line, cat, entry_id))
                    n_pending += 1
                for ev in e.get("events", []):
                    cur.execute(
                        "INSERT INTO events(title,description,event_type,date,importance,source_entry)"
                        " VALUES(%s,%s,%s,%s,%s,%s)",
                        (ev.get("title", ""), ev.get("description", ""), ev.get("type", "life"),
                         e.get("date", ""), int(ev.get("importance", 1) or 1), entry_id))
            prof = d.get("profile", {})
            old_sections = prof.get("sections") or prof  # 兼容原型格式 {节名: [..]}
            for sec, items in old_sections.items():
                if sec not in config.SECTIONS or not isinstance(items, list):
                    continue
                for item in items:
                    cur.execute("SELECT 1 FROM archive WHERE section=%s AND text=%s",
                                (sec, item))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO archive(section,text) VALUES(%s,%s)",
                                    (sec, item))
            for p in d.get("pending", []):
                cur.execute("INSERT INTO pending_memories(text,cat) VALUES(%s,%s)",
                            (p.get("text", ""), p.get("cat", "长期背景")))
                n_pending += 1
            for t in d.get("todos", []):
                cur.execute("INSERT INTO todos(text,due,done) VALUES(%s,%s,%s)",
                            (t.get("text", ""), t.get("due", ""), 1 if t.get("done") else 0))
                n_todos += 1
            for g in d.get("goals", []):
                cur.execute("INSERT INTO goals(title,description,status,progress,deadline) VALUES(%s,%s,%s,%s,%s)",
                            (g.get("title", ""), g.get("description", ""),
                             g.get("status", "active"), int(g.get("progress", 0) or 0),
                             g.get("deadline", "")))
    return {"entries": n_entries, "pending": n_pending, "todos": n_todos}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
