import json
import os
from datetime import datetime

import psycopg2.extras
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, db, export, llm, parser

app = FastAPI(title="拾光")

STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


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


@app.post("/api/process", dependencies=[Depends(auth.require_login)])
def process_log(body: ProcessBody):
    if not body.raw and not body.mode:
        raise HTTPException(status_code=400, detail="口述内容和模式至少填一个")
    date = body.date or datetime.now().strftime("%Y-%m-%d")
    with db.db() as conn:
        sections = db.archive_sections(conn)["sections"]
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM entries ORDER BY id")
            entries = [db.entry_to_dict(r) for r in cur.fetchall()]
    prompt = llm.build_user_prompt(sections, entries, body.raw.strip(), date, body.mode)
    try:
        reply = llm.call_glm(prompt)
    except llm.LLMError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    parsed = parser.parse_result(reply, body.raw, date, body.mode)
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "INSERT INTO entries(date,mode,raw,polished,summary,advice,mempack,profile_delta,evidence,llm_reply)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (parsed["date"], parsed["mode"], parsed["raw"], parsed["polished"],
                 parsed["summary"], json.dumps(parsed["advice"], ensure_ascii=False),
                 json.dumps(parsed["memPack"], ensure_ascii=False),
                 parsed["profileDelta"], parsed["evi"], reply))
            row = cur.fetchone()
            entry_id = row["id"]
            for line in parsed["memPack"]:
                cat, txt = parser.split_mem_line(line)
                if cat is None:
                    cat = parser.guess_cat(txt)
                cur.execute(
                    "INSERT INTO pending_memories(text,cat,src_entry,evi) VALUES(%s,%s,%s,%s)",
                    (txt, cat, entry_id, parsed["evi"]))
        return db.entry_to_dict(row)


# ---------- entries ----------

@app.get("/api/entries", dependencies=[Depends(auth.require_login)])
def list_entries(limit: int = 500, offset: int = 0):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM entries ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset))
            return [db.entry_to_dict(r) for r in cur.fetchall()]


@app.get("/api/entries/{eid}", dependencies=[Depends(auth.require_login)])
def get_entry(eid: int):
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM entries WHERE id=%s", (eid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="日志不存在")
            return db.entry_to_dict(row)


@app.delete("/api/entries/{eid}", dependencies=[Depends(auth.require_login)])
def delete_entry(eid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM entries WHERE id=%s", (eid,))
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
                cur.execute("INSERT INTO archive(section,text) VALUES(%s,%s)",
                            (body.cat, row["text"]))
            cur.execute("UPDATE pending_memories SET status='archived', cat=%s WHERE id=%s",
                        (body.cat, pid))
    return {"ok": True}


@app.post("/api/pending/{pid}/skip", dependencies=[Depends(auth.require_login)])
def skip_pending(pid: int):
    with db.db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_memories SET status='discarded' WHERE id=%s", (pid,))
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
        with conn.cursor() as cur:
            cur.execute("DELETE FROM archive")
            for sec, items in sections.items():
                for item in items:
                    cur.execute("INSERT INTO archive(section,text,updated) VALUES(%s,%s,%s)",
                                (sec, item, now))
    return {"ok": True}


# ---------- export / import ----------

def _export_data():
    with db.db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pending_memories WHERE status='pending' ORDER BY id")
            pending = [db.pending_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM todos ORDER BY id")
            todos = [db.todo_to_dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM entries ORDER BY id")
            rows = [db.entry_to_dict(r) for r in cur.fetchall()]
        return {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "profile": db.archive_sections(conn),
            "pending": pending, "todos": todos, "rows": rows,
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
    return {"entries": n_entries, "pending": n_pending, "todos": n_todos}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
