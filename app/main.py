import json
import os
import time
import uuid
import re
from datetime import datetime, timedelta

import psycopg2.extras
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, db, export, llm, parser, dates

app = FastAPI(title="拾光")


@app.middleware("http")
async def no_cache_html(request, call_next):
    """入口 HTML 不缓存(静态 js/css 靠 ?v= 版本号失效):
    手机浏览器/运营 商代理缓存 index.html 会让用户永远看到旧版应用。"""
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        resp.headers["Cache-Control"] = "no-cache"
    return resp

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
         "LEFT JOIN archive ar ON e.source_type='memory' AND ar.id=e.source_id "
         "WHERE ((e.source_type='entry' AND en.id IS NOT NULL"
         " AND e.content=LEFT(COALESCE(NULLIF(en.polished,''),en.raw,''),2000))"
         " OR (e.source_type='memory' AND ar.status='active')) "
         "AND (%s IS NULL OR e.source_id != %s OR e.source_type != 'entry') "
         "ORDER BY dist LIMIT 50")
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(q, (_vec_literal(query_vec), exclude_entry, exclude_entry))
        rows = cur.fetchall()
    today = datetime.fromisoformat(dates.today()).date()
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
def login(body: LoginBody, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    rec = _login_fails.get(ip)
    if rec and rec[0] >= 5 and time.time() - rec[1] < 600:
        raise HTTPException(status_code=429, detail="尝试次数过多,请 10 分钟后再试")
    if not auth.verify_password(body.password):
        fails = (rec[0] + 1) if rec else 1
        _login_fails[ip] = (fails, time.time())
        raise HTTPException(status_code=401, detail="密码不对")
    _login_fails.pop(ip, None)
    response.set_cookie(auth.COOKIE, auth.make_token(), max_age=auth.TTL,
                        httponly=True, samesite="lax", secure=True)
    return {"ok": True}


_login_fails = {}  # ip -> (连续失败次数, 最近失败时间)


@app.get("/api/health")
def health():
    """无鉴权健康检查:keepalive 用它保持 Render 唤醒 + Supabase 不休眠(会真实连库)。"""
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    return {"ok": True, "version": os.environ.get("RENDER_GIT_COMMIT", "local")}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


# ---------- process ----------

class ProcessBody(BaseModel):
    raw: str = ""
    mode: str = ""
    date: str = ""
    request_id: str = ""


def _job_update(jid, status=None, stage=None, error=None, entry_id=None, attempt=None):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status=COALESCE(%s,status), stage=COALESCE(%s,stage),"
                " error=COALESCE(%s,error), entry_id=COALESCE(%s,entry_id),"
                " updated_at=to_char(now() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
                " WHERE id=%s AND (%s IS NULL OR (attempt=%s AND status='running'))",
                (status, stage, error, entry_id, jid, attempt, attempt))


@app.post("/api/process", dependencies=[Depends(auth.require_login)])
def process_log(body: ProcessBody, tasks: BackgroundTasks):
    modes = ('', '/只润色', '/快速', '/深度复盘', '/更新记忆', '/查看画像', '/周总结', '/月总结', '/整理计划')
    if body.mode not in modes:
        raise HTTPException(status_code=400, detail="不支持的处理模式")
    if not body.raw.strip() and body.mode not in ('/查看画像', '/周总结', '/月总结'):
        raise HTTPException(status_code=400, detail="口述内容和模式至少填一个")
    if body.date and not dates.valid_date(body.date):
        raise HTTPException(status_code=400, detail="日志日期须为 YYYY-MM-DD")
    if body.request_id and not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', body.request_id):
        raise HTTPException(status_code=400, detail="提交标识不合法")
    jid = body.request_id or uuid.uuid4().hex
    requested_date = body.date
    body.date = body.date or dates.today()
    # 原文随任务立即落库:处理失败/重启也不丢口述内容(第一原则:原始日志永久保留)
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("INSERT INTO jobs(id,status,raw,mode,date) VALUES(%s,'queued',%s,%s,%s)"
                        " ON CONFLICT(id) DO NOTHING RETURNING id",
                        (jid, body.raw, body.mode, body.date))
            if not cur.fetchone():
                cur.execute("SELECT raw,mode,date FROM jobs WHERE id=%s", (jid,))
                old = cur.fetchone()
                if old['raw'] != body.raw or old['mode'] != body.mode or (requested_date and old['date'] != requested_date):
                    raise HTTPException(status_code=409, detail="提交标识已用于不同内容")
                return {"jobId": jid}
    tasks.add_task(_run_process, jid, body)
    return {"jobId": jid}


@app.get("/api/jobs", dependencies=[Depends(auth.require_login)])
def list_jobs():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("UPDATE jobs SET status='failed', error='处理已中断，原文已保存，可重新处理'"
                        " WHERE status IN ('queued','running') AND"
                        " COALESCE(NULLIF(updated_at,''),created_at)::timestamp <"
                        " (now() AT TIME ZONE 'Asia/Shanghai') - INTERVAL '20 minutes'")
            cur.execute("SELECT id,status,stage,error,raw,mode,date,created_at FROM jobs"
                        " WHERE status IN ('queued','running','failed') ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


@app.post("/api/jobs/{jid}/retry", dependencies=[Depends(auth.require_login)])
def retry_job(jid: str, tasks: BackgroundTasks):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("UPDATE jobs SET status='queued',stage='',error='',preview='',attempt=attempt+1,"
                        " updated_at=to_char(now() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
                        " WHERE id=%s AND status='failed' AND entry_id IS NULL RETURNING *", (jid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="任务已在处理或已完成，请刷新查看")
            body = ProcessBody(raw=row['raw'] or '', mode=row['mode'] or '',
                               date=row['date'] or row['created_at'][:10])
    tasks.add_task(_run_process, jid, body, row['attempt'])
    return {"jobId": jid}


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth.require_login)])
def get_job(jid: str):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM jobs WHERE id=%s", (jid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="任务不存在或已过期")
            # 僵尸任务收割:queued/running 超过 20 分钟(服务重启/进程死亡),标记失败。
            # 时间比较全部在库端用上海时区完成 —— 进程本地时区不可靠(Render 是 UTC)。
            if row["status"] in ("queued", "running"):
                cur.execute(
                    "UPDATE jobs SET status='failed',"
                    " error='任务超时(服务可能重启过),请重新提交'"
                    " WHERE id=%s AND status IN ('queued','running')"
                    " AND (now() AT TIME ZONE 'Asia/Shanghai') - INTERVAL '20 minutes'"
                    " > COALESCE(NULLIF(updated_at,''), created_at)::timestamp", (jid,))
                if cur.rowcount:
                    conn.commit()
                    row = {**dict(row), "status": "failed",
                           "error": "任务超时(服务可能重启过),请重新提交"}
            out = {"id": row["id"], "status": row["status"], "stage": row["stage"],
                   "error": row["error"], "entryId": row["entry_id"],
                   "raw": row["raw"] or "", "preview": row["preview"] or ""}
            if row["status"] == "done" and row["entry_id"]:
                cur.execute("SELECT * FROM entries WHERE id=%s", (row["entry_id"],))
                e = cur.fetchone()
                if e:
                    cur.execute("SELECT * FROM events WHERE source_entry=%s"
                                " ORDER BY importance DESC", (row["entry_id"],))
                    out["entry"] = db.entry_to_dict(e)
                    out["entry"]["events"] = [db.event_to_dict(r) for r in cur.fetchall()]
            return out


def _run_process(jid, body, attempt=0):
    try:
        with db.db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET status='running',"
                            " updated_at=to_char(now() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
                            " WHERE id=%s AND status='queued' AND attempt=%s RETURNING id", (jid, attempt))
                if not cur.fetchone():
                    return
        _process(jid, body, attempt)
    except llm.LLMError as e:
        _job_update(jid, status="failed", error=str(e), attempt=attempt)
    except Exception as e:  # 兜底:任务异常不能丢
        _job_update(jid, status="failed", error="处理异常(%s)，原文已保存，可重新处理" % type(e).__name__, attempt=attempt)


def _process(jid, body, attempt=0):
    date = body.date or dates.today()

    # ① embedding + Context Builder:档案 + 活跃目标 + 最近 3 条 + 语义相关 5 条
    _job_update(jid, stage="embedding", attempt=attempt)
    query_vec = None
    with db.db() as conn:
        sections = db.archive_sections(conn, for_prompt=True)["sections"]
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM goals WHERE status='active' ORDER BY id")
            goals = [db.goal_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM entries WHERE mode NOT IN ('/周总结','/月总结','/查看画像')"
                        " ORDER BY date DESC, id DESC LIMIT 3")
            recent = [db.entry_to_dict(r) for r in cur.fetchall()][::-1]
        if body.raw and body.mode != '/只润色' and db.vector_ok():
            try:
                query_vec = llm.embed_texts([body.raw[:1500]])[0]
            except llm.LLMError:
                query_vec = None
        related = search_related(conn, query_vec, top_k=8)  # 跨时间关联是核心能力,多召回几条
        # 周/月总结按日期范围取全量记录:语义检索召回的只是相关子集,总结必须覆盖整个期间
        period_entries = None
        if body.mode in ("/周总结", "/月总结"):
            since, until = dates.period_window(body.mode, date)
            with conn.cursor() as cur:
                cur.execute("SELECT id, date, polished, raw FROM entries WHERE date >= %s AND date <= %s"
                            " AND COALESCE(NULLIF(mode,''),'') NOT IN ('/周总结','/月总结','/查看画像')"
                            " ORDER BY date, id", (since, until))
                period_entries = [{"id": r[0], "date": r[1], "text": r[2] or r[3] or ""}
                                  for r in cur.fetchall()]

    prompt = llm.build_user_prompt(sections, goals, recent, related,
                                   body.raw.strip(), date, body.mode,
                                   period_entries=period_entries,
                                   period_range=dates.period_window(body.mode, date) if period_entries is not None else None)
    deep = body.mode in llm.DEEP_MODES
    _job_update(jid, stage="generating", attempt=attempt)
    last_preview = [0.0, '']
    def save_preview(text):
        if not text or text == last_preview[1] or time.monotonic()-last_preview[0] < 2:
            return
        try:
            with db.db() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE jobs SET preview=%s WHERE id=%s AND status='running' AND attempt=%s",
                                   (text, jid, attempt))
            last_preview[:] = [time.monotonic(), text]
        except Exception:
            pass  # Preview failure must not discard the final result.
    reply = llm.call_glm(prompt, deep=deep, on_preview=save_preview, mode=body.mode)  # LLMError 由 _run_process 记入 jobs
    _job_update(jid, stage="saving", attempt=attempt)
    parsed = parser.parse_json_result(reply, body.raw, date, body.mode)

    # ② 入库:entry + 待审核记忆 + 事件,同一事务;向量入库在事务提交后单独尝试
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Lock the job through the save transaction; obsolete attempts cannot archive.
            cur.execute("SELECT status,attempt FROM jobs WHERE id=%s FOR UPDATE", (jid,))
            job = cur.fetchone()
            if not job or job['status'] != 'running' or job['attempt'] != attempt:
                return None
            cur.execute(
                "INSERT INTO entries(date,mode,raw,polished,summary,understanding,growth,takeaway,advice,mempack,profile_delta,evidence,llm_reply,prompt_version,model)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (parsed["date"], parsed["mode"], parsed["raw"], parsed["polished"],
                 parsed["summary"], parsed.get("understanding", ""),
                 parsed.get("growth", ""), parsed.get("takeaway", ""),
                 json.dumps(parsed["advice"], ensure_ascii=False),
                 json.dumps([m["text"] for m in parsed["memories"]], ensure_ascii=False),
                 parsed["profileDelta"], parsed["evi"], reply,
                 llm.PROMPT_VERSION, llm.model_for(deep)))
            row = cur.fetchone()
            entry_id = row["id"]
            cur.execute("UPDATE entries SET processing_warning=%s WHERE id=%s",
                        (parsed.get('warning', ''), entry_id))
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
                     ev.get('date') or parsed["date"], ev["importance"], entry_id))
            cur.execute("UPDATE jobs SET status='done',stage='saving',entry_id=%s,error='',"
                        " updated_at=to_char(now() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
                        " WHERE id=%s", (entry_id, jid))
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

def _entries_with_events(conn, limit=500):
    """limit=None 时不截断(备份导出必须全量)。"""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM entries ORDER BY id DESC" +
                    (" LIMIT %d" % limit if limit else ""))
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


class EntryPatch(BaseModel):
    polished: str


@app.patch("/api/entries/{eid}", dependencies=[Depends(auth.require_login)])
def patch_entry(eid: int, body: EntryPatch):
    """正文纠错:只改 polished,raw 原口述保留;向量同步重建(失败不影响保存)。"""
    text = body.polished.strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文不能为空")
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE entries SET polished=%s,derived_stale=TRUE WHERE id=%s RETURNING id", (text, eid))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="日志不存在")
        conn.commit()
        if db.vector_ok():
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM embeddings WHERE source_type='entry' AND source_id=%s", (eid,))
                conn.commit()
                vec = llm.embed_texts([text[:1500]])[0]
                store_embedding(conn, "entry", eid, text[:2000], vec)
                conn.commit()
            except Exception:
                conn.rollback()  # 只回滚向量部分
    return {"ok": True}


@app.delete("/api/entries/{eid}", dependencies=[Depends(auth.require_login)])
def delete_entry(eid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM entries WHERE id=%s", (eid,))
            if db.vector_ok():
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
    text: str | None = None


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
            row = dict(row)
            if body.text is not None:
                if not body.text.strip():
                    raise HTTPException(status_code=400, detail="记忆内容不能为空")
                row['text'] = body.text.strip()
            if row['src_entry']:
                cur.execute("SELECT derived_stale FROM entries WHERE id=%s", (row['src_entry'],))
                source = cur.fetchone()
                if source and source['derived_stale'] and body.text is None:
                    raise HTTPException(status_code=409, detail="来源正文已修改，请核对并提交记忆文字")
            cur.execute("SELECT id, source_entries FROM archive WHERE section=%s AND text=%s",
                        (body.cat, row["text"]))
            existing = cur.fetchone()
            today = dates.today()
            # Semantic similarity cannot prove equivalence (especially negation).
            # Only exact, same-section matches are merged automatically.
            if existing:
                # 同一记忆再次被确认:累积证据链而不是跳过
                srcs = list(existing["source_entries"] or [])
                if row["src_entry"] and row["src_entry"] not in srcs:
                    srcs.append(row["src_entry"])
                cur.execute("UPDATE archive SET source_entries=%s, last_verified=%s,status='active' WHERE id=%s",
                            (srcs, today, existing["id"]))
                aid = existing["id"]
            else:
                src = [row["src_entry"]] if row["src_entry"] else []
                cur.execute(
                    "INSERT INTO archive(section,text,source_entries,confidence,last_verified)"
                    " VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (body.cat, row["text"], src, row["confidence"], today))
                aid = cur.fetchone()["id"]
            cur.execute("UPDATE pending_memories SET status='archived', cat=%s,text=%s WHERE id=%s",
                        (body.cat, row['text'], pid))
        conn.commit()  # 核心数据(确认的记忆)先落库;向量只是索引,失败不应回滚事实
        if not existing and db.vector_ok():
            # 记忆也进向量库,供语义检索;独立事务,失败可接受
            try:
                vec = llm.embed_texts([row["text"][:1000]])[0]
                store_embedding(conn, "memory", aid, row["text"], vec)
                conn.commit()
            except Exception:
                conn.rollback()
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
            if body.status == "deprecated" and db.vector_ok():
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
        prof = db.archive_sections(conn)
        # 证据链:每条记忆的来源日志(日期 + 标题摘要)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            for item in prof["items"]:
                srcs = item.get("sources") or []
                if not srcs:
                    item["evidence"] = []
                    continue
                cur.execute("SELECT id, date, polished, summary FROM entries WHERE id = ANY(%s)",
                            (srcs,))
                by_id = {r["id"]: r for r in cur.fetchall()}
                item["evidence"] = [
                    {"id": sid, "date": by_id[sid]["date"] if sid in by_id else "",
                     "title": ((by_id[sid]["polished"] or by_id[sid]["summary"] or "")
                               .strip().split("\n")[0][:50]) if sid in by_id else "(已删除)"}
                    for sid in srcs if sid in by_id]
        return prof


class ProfileBody(BaseModel):
    text: str  # "## 节名" 格式的整段文本,服务端解析


@app.put("/api/profile", dependencies=[Depends(auth.require_login)])
def put_profile(body: ProfileBody):
    if any('## ' + section not in body.text for section in config.SECTIONS):
        raise HTTPException(status_code=400, detail="请保留全部六个节标题，避免误清空档案")
    sections = parser.parse_profile_textarea(body.text)
    now = datetime.now(dates.SHANGHAI).strftime("%Y-%m-%d %H:%M")
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id,section,text,status FROM archive FOR UPDATE")
            previous = list(cur.fetchall())
            kept = set()
            aids = []
            for sec, items in sections.items():
                for item in dict.fromkeys(items):
                    old = next((r for r in previous if r['section'] == sec and r['text'] == item), None)
                    if old:
                        kept.add(old['id'])
                        cur.execute("UPDATE archive SET status='active' WHERE id=%s", (old['id'],))
                        continue
                    cur.execute("INSERT INTO archive(section,text,updated) VALUES(%s,%s,%s) RETURNING id",
                                (sec, item, now))
                    aids.append((cur.fetchone()["id"], item))
            for old in previous:
                if old['id'] not in kept and old['status'] == 'active':
                    cur.execute("UPDATE archive SET status='deprecated',updated=%s WHERE id=%s", (now, old['id']))
                    if db.vector_ok():
                        cur.execute("DELETE FROM embeddings WHERE source_type='memory' AND source_id=%s", (old['id'],))
        conn.commit()  # 档案本体先落库;向量重建失败不回滚档案
        if db.vector_ok():
            for aid, item in aids:
                try:
                    vec = llm.embed_texts([item[:1000]])[0]
                    store_embedding(conn, "memory", aid, item, vec)
                    conn.commit()
                except Exception:
                    conn.rollback()
    return {"ok": True}


# ---------- export / import ----------

def _export_data():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pending_memories ORDER BY id")
            pending = [db.pending_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM todos ORDER BY id")
            todos = [db.todo_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM events ORDER BY id")
            events = [db.event_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM goals ORDER BY id")
            goals = [db.goal_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT id,raw,mode,date,created_at FROM jobs WHERE status IN ('queued','running','failed')")
            unfinished = [dict(r) for r in cur.fetchall()]
            rows = _entries_with_events(conn, limit=None)  # 备份必须全量,不截断
        return {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "profile": db.archive_sections(conn),
            "pending": pending, "todos": todos, "events": events, "goals": goals,
            "rows": rows,
            "unfinishedJobs": unfinished,
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


def _import_event(cur, ev, fallback_date, entry_id):
    """导入单条事件,(title,date,description) 判重 —— 删除过日志后重导入不翻倍。"""
    title = ev.get("title", "")
    date = ev.get("date") or fallback_date or ""
    desc = ev.get("description", "")
    cur.execute("SELECT 1 FROM events WHERE title=%s AND COALESCE(date,'')=%s"
                " AND COALESCE(description,'')=%s", (title, date, desc))
    if cur.fetchone():
        return False
    cur.execute(
        "INSERT INTO events(title,description,event_type,date,importance,source_entry)"
        " VALUES(%s,%s,%s,%s,%s,%s)",
        (title, desc, ev.get("type", "life"), date,
         int(ev.get("importance", 1) or 1), entry_id))
    return True


@app.post("/api/import", dependencies=[Depends(auth.require_login)])
async def import_backup(request: Request):
    try:
        d = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 解析失败")
    if not isinstance(d, dict) or "entries" not in d or "profile" not in d:
        raise HTTPException(status_code=400, detail="不是有效的拾光备份格式")
    n_entries = n_pending = n_todos = 0
    entry_map = {}
    imported_events = {
        (ev.get('title', ''), ev.get('description', ''), ev.get('date') or '')
        for entry in d.get('entries', []) for ev in entry.get('events', [])
    }  # 顶层是内嵌事件的副本；兼容旧备份两处都缺日期的情形。
    # (含各日志内嵌的),同一次导入不能插两遍
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            for e in d.get("entries", []):
                # 幂等:同一 (date, raw) 的日志已存在则跳过,重复导入不翻倍;
                # 纯模式条目(raw 为空)用 (date, polished) 判重
                if e.get("raw"):
                    cur.execute("SELECT id FROM entries WHERE date=%s AND raw=%s AND mode=%s ORDER BY id LIMIT 1",
                                (e.get("date", ""), e.get("raw", ""), e.get('mode', '')))
                    existing = cur.fetchone()
                    if existing:
                        entry_map[e.get('id')] = existing['id']
                        continue
                elif e.get("polished"):
                    cur.execute("SELECT id FROM entries WHERE date=%s AND polished=%s AND mode=%s ORDER BY id LIMIT 1",
                                (e.get("date", ""), e.get("polished", ""), e.get('mode', '')))
                    existing = cur.fetchone()
                    if existing:
                        entry_map[e.get('id')] = existing['id']
                        continue
                cur.execute(
                    "INSERT INTO entries(date,mode,raw,polished,summary,understanding,growth,takeaway,advice,mempack,profile_delta,evidence,prompt_version,model)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (e.get("date", ""), e.get("mode", ""), e.get("raw", ""),
                     e.get("polished", ""), e.get("summary", ""),
                     e.get("understanding", ""), e.get("growth", ""),
                     e.get("takeaway", ""),
                     json.dumps(e.get("advice", []), ensure_ascii=False),
                     json.dumps(e.get("memPack", e.get("mempack", [])), ensure_ascii=False),
                     e.get("profileDelta", e.get("profile_delta", "")),
                     e.get("evi", e.get("evidence", "")),
                     e.get("promptVersion", e.get("prompt_version", "")),
                     e.get("model", "")))
                entry_id = cur.fetchone()["id"]
                entry_map[e.get('id')] = entry_id
                cur.execute("UPDATE entries SET processing_warning=%s,derived_stale=%s WHERE id=%s",
                            (e.get('warning', ''), bool(e.get('derivedStale')), entry_id))
                n_entries += 1
                for m in (e.get("memPack", e.get("mempack", [])) if d.get('formatVersion', 1) < 2 else []):
                    line = m if isinstance(m, str) else m.get("text", "")
                    cat, line = parser.split_mem_line(line)
                    if cat is None:
                        cat = parser.guess_cat(line)
                    # 判重不看 status:删除过日志/确认过记忆后重导入,同文本不复活
                    cur.execute("SELECT 1 FROM pending_memories WHERE text=%s", (line,))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO pending_memories(text,cat,src_entry) VALUES(%s,%s,%s)",
                            (line, cat, entry_id))
                        n_pending += 1
                for ev in e.get("events", []):
                    if _import_event(cur, ev, e.get("date", ""), entry_id):
                        imported_events.add((ev.get("title", ""), ev.get("description", ""), ev.get('date') or e.get('date','')))
            # 顶层 events:导入未被任何日志吸收的孤儿事件(删除过日志的遗留,
            # _entries_with_events 只把挂在现存日志上的事件放进 entries[].events)
            for ev in d.get("events", []):
                if (ev.get("title", ""), ev.get("description", ""), ev.get('date') or '') in imported_events:
                    continue
                _import_event(cur, ev, "", entry_map.get(ev.get('src')))
            prof = d.get("profile", {})
            # 优先用带证据链的 items(v1.2 备份),保真 source/confidence/lastVerified/status
            if isinstance(prof.get("items"), list) and prof["items"]:
                for item in prof["items"]:
                    if item.get("section") not in config.SECTIONS or not item.get("text"):
                        continue
                    cur.execute("SELECT 1 FROM archive WHERE section=%s AND text=%s",
                                (item["section"], item["text"]))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO archive(section,text,source_entries,confidence,last_verified,status)"
                            " VALUES(%s,%s,%s,%s,%s,%s)",
                            (item["section"], item["text"],
                             list(dict.fromkeys(entry_map[s] for s in (item.get("sources") or []) if s in entry_map and s is not None)), item.get("confidence"),
                             item.get("lastVerified") or "", item.get("status") or "active"))
            else:
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
                if p.get("text"):
                    # 判重不看 status:已确认/已跳过的记忆重导入时不复活
                    cur.execute("SELECT 1 FROM pending_memories WHERE text=%s",
                                (p.get("text", ""),))
                    if not cur.fetchone():
                        status = p.get('status', 'pending')
                        if status not in ('pending', 'archived', 'discarded'): status = 'pending'
                        cur.execute("INSERT INTO pending_memories(text,cat,src_entry,status,evi,confidence,note) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                                    (p.get("text", ""), p.get("cat", "长期背景"), entry_map.get(p.get('src')),
                                     status, p.get('evi',''), p.get('confidence'), p.get('note','')))
                        n_pending += 1
            for t in d.get("todos", []):
                if t.get("text"):
                    cur.execute("SELECT 1 FROM todos WHERE text=%s AND due=%s",
                                (t.get("text", ""), t.get("due", "")))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO todos(text,due,done,src_entry) VALUES(%s,%s,%s,%s)",
                                    (t.get("text", ""), t.get("due", ""),
                                     1 if t.get("done") else 0, entry_map.get(t.get('src'))))
                        n_todos += 1
            for g in d.get("goals", []):
                if g.get("title"):
                    # 判重只看 title:目标推进到 done 后重导入不产生重复的 active 目标
                    cur.execute("SELECT 1 FROM goals WHERE title=%s",
                                (g.get("title", ""),))
                    if cur.fetchone():
                        continue
                cur.execute("INSERT INTO goals(title,description,status,progress,deadline) VALUES(%s,%s,%s,%s,%s)",
                            (g.get("title", ""), g.get("description", ""),
                             g.get("status", "active"), int(g.get("progress", 0) or 0),
                             g.get("deadline", "")))
            for job in d.get('unfinishedJobs', []):
                jid = job.get('id', '')
                if not isinstance(jid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', jid):
                    continue
                cur.execute("INSERT INTO jobs(id,status,raw,mode,date,error) VALUES(%s,'failed',%s,%s,%s,%s)"
                            " ON CONFLICT(id) DO NOTHING",
                            (jid, job.get('raw',''), job.get('mode',''), job.get('date') or dates.today(),
                             '从备份找回的未完成记录，请核对后重新处理'))
    return {"entries": n_entries, "pending": n_pending, "todos": n_todos}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
