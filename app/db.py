import json
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  date TEXT NOT NULL,
  mode TEXT DEFAULT '',
  raw TEXT DEFAULT '',
  polished TEXT DEFAULT '',
  summary TEXT DEFAULT '',
  advice TEXT DEFAULT '[]',
  mempack TEXT DEFAULT '[]',
  profile_delta TEXT DEFAULT '',
  evidence TEXT DEFAULT '',
  llm_reply TEXT DEFAULT '',
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS pending_memories (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  text TEXT NOT NULL,
  cat TEXT NOT NULL,
  src_entry INTEGER REFERENCES entries(id) ON DELETE SET NULL,
  evi TEXT DEFAULT '',
  confidence REAL,
  note TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS archive (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  section TEXT NOT NULL,
  text TEXT NOT NULL,
  updated TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS')),
  source_entries INTEGER[] DEFAULT '{}',
  confidence REAL,
  last_verified TEXT DEFAULT '',
  status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT DEFAULT '',
  error TEXT DEFAULT '',
  entry_id INTEGER,
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS')),
  updated_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS todos (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  text TEXT NOT NULL,
  due TEXT DEFAULT '',
  done INTEGER DEFAULT 0,
  src_entry INTEGER,
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  event_type TEXT DEFAULT 'life',
  date TEXT,
  importance INT DEFAULT 1,
  source_entry INTEGER REFERENCES entries(id) ON DELETE SET NULL,
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  progress INT DEFAULT 0,
  deadline TEXT DEFAULT '',
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
"""

VECTOR_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS embeddings (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  embedding vector(%(dim)s)
);
CREATE INDEX IF NOT EXISTS embeddings_idx ON embeddings USING hnsw (embedding vector_cosine_ops);
"""


# 已有库的结构迁移(幂等)
MIGRATIONS = [
    "ALTER TABLE archive ADD COLUMN IF NOT EXISTS source_entries INTEGER[] DEFAULT '{}'",
    "ALTER TABLE archive ADD COLUMN IF NOT EXISTS confidence REAL",
    "ALTER TABLE archive ADD COLUMN IF NOT EXISTS last_verified TEXT DEFAULT ''",
    "ALTER TABLE pending_memories ADD COLUMN IF NOT EXISTS confidence REAL",
    "ALTER TABLE pending_memories ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
    "ALTER TABLE archive ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
]


def connect():
    if not config.DATABASE_URL:
        raise RuntimeError("未配置 DATABASE_URL(Supabase PostgreSQL 连接串)")
    import time
    last = None
    for i in range(3):  # 网络抖动重试
        try:
            return psycopg2.connect(config.DATABASE_URL, connect_timeout=15)
        except psycopg2.OperationalError as e:
            last = e
            time.sleep(1 + i)
    raise last


@contextmanager
def db():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            for stmt in MIGRATIONS:
                cur.execute(stmt)
        conn.commit()  # 基础表先落库,避免 pgvector 探测失败被连带回滚
        global VECTOR_OK
        if VECTOR_OK is None:
            try:
                with conn.cursor() as cur:
                    cur.execute(VECTOR_SCHEMA, {"dim": config.EMBED_DIM})
                VECTOR_OK = True
            except Exception:
                conn.rollback()
                VECTOR_OK = False
        yield conn
        conn.commit()
    finally:
        conn.close()


VECTOR_OK = None  # None=未探测 True/False=pgvector 可用性


def vector_ok():
    return VECTOR_OK is True


def rows_to_dicts(cur):
    return [dict(r) for r in cur.fetchall()]


def entry_to_dict(row):
    if isinstance(row, psycopg2.extras.DictRow):
        row = dict(row)
    return {
        "id": row["id"], "date": row["date"], "mode": row["mode"], "raw": row["raw"],
        "polished": row["polished"], "summary": row["summary"],
        "advice": json.loads(row["advice"]), "mempack": json.loads(row["mempack"]),
        "profileDelta": row["profile_delta"], "evi": row["evidence"],
        "createdAt": row["created_at"],
    }


def pending_to_dict(row):
    if isinstance(row, psycopg2.extras.DictRow):
        row = dict(row)
    return {"id": row["id"], "text": row["text"], "cat": row["cat"],
            "src": row["src_entry"], "evi": row["evi"],
            "confidence": row.get("confidence"), "note": row.get("note", "")}


def todo_to_dict(row):
    if isinstance(row, psycopg2.extras.DictRow):
        row = dict(row)
    return {"id": row["id"], "text": row["text"], "due": row["due"],
            "done": bool(row["done"]), "src": row["src_entry"]}


def event_to_dict(row):
    if isinstance(row, psycopg2.extras.DictRow):
        row = dict(row)
    return {"id": row["id"], "title": row["title"], "description": row["description"],
            "type": row["event_type"], "date": row["date"],
            "importance": row["importance"], "src": row["source_entry"]}


def goal_to_dict(row):
    if isinstance(row, psycopg2.extras.DictRow):
        row = dict(row)
    return {"id": row["id"], "title": row["title"], "description": row["description"],
            "status": row["status"], "progress": row["progress"],
            "deadline": row["deadline"], "createdAt": row["created_at"]}


SECTION_CAPS = {"长期背景": 15, "当前目标": 10, "长期偏好": 10,
                "进行中的项目": 10, "经多次观察得到的模式": 8, "近期变化": 8}


def archive_sections(conn, for_prompt=False):
    """for_prompt=True 时只取 active 且按节限量(Context Builder 预算)。"""
    sections = {s: [] for s in config.SECTIONS}
    items = []
    updated = ""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id, section, text, updated, source_entries, confidence,"
                    " last_verified, status FROM archive ORDER BY id")
        for row in cur.fetchall():
            active = (row["status"] or "active") == "active"
            if row["section"] in sections and active and \
                    len(sections[row["section"]]) < (SECTION_CAPS[row["section"]] if for_prompt else 10 ** 6):
                sections[row["section"]].append(row["text"])
            items.append({"id": row["id"], "section": row["section"], "text": row["text"],
                          "sources": list(row["source_entries"] or []),
                          "confidence": row["confidence"],
                          "lastVerified": row["last_verified"] or "",
                          "status": row["status"] or "active"})
            if row["updated"] and row["updated"] > updated:
                updated = row["updated"]
    return {"sections": sections, "items": items, "updated": updated}
