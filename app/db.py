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
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS archive (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  section TEXT NOT NULL,
  text TEXT NOT NULL,
  updated TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE TABLE IF NOT EXISTS todos (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  text TEXT NOT NULL,
  due TEXT DEFAULT '',
  done INTEGER DEFAULT 0,
  src_entry INTEGER,
  created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS'))
);
"""


def connect():
    if not config.DATABASE_URL:
        raise RuntimeError("未配置 DATABASE_URL(Supabase PostgreSQL 连接串)")
    return psycopg2.connect(config.DATABASE_URL, connect_timeout=15)


@contextmanager
def db():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


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
            "src": row["src_entry"], "evi": row["evi"]}


def todo_to_dict(row):
    if isinstance(row, psycopg2.extras.DictRow):
        row = dict(row)
    return {"id": row["id"], "text": row["text"], "due": row["due"],
            "done": bool(row["done"]), "src": row["src_entry"]}


def archive_sections(conn):
    sections = {s: [] for s in config.SECTIONS}
    updated = ""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT section, text, updated FROM archive ORDER BY id")
        for row in cur.fetchall():
            if row["section"] in sections:
                sections[row["section"]].append(row["text"])
            if row["updated"] and row["updated"] > updated:
                updated = row["updated"]
    return {"sections": sections, "updated": updated}
