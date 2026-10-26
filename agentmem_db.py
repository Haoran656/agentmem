# -*- coding: utf-8 -*-
"""agentmem_db.py — 建库/连接/schema 迁移（L2 统一事件索引）"""
import sqlite3
from pathlib import Path

AM_DIR = Path(__file__).resolve().parent          # <KB>\agentmem
DB_PATH = AM_DIR / "agentmem.db"
BACKUP_DIR = AM_DIR / "db-backups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  agent        TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  project      TEXT,
  project_name TEXT,
  title        TEXT,
  started_at   INTEGER,
  ended_at     INTEGER,
  models       TEXT,
  n_events     INTEGER DEFAULT 0,
  in_tokens    INTEGER DEFAULT 0,
  out_tokens   INTEGER DEFAULT 0,
  cache_tokens INTEGER DEFAULT 0,
  cost_usd     REAL DEFAULT 0,
  cost_cny     REAL DEFAULT 0,
  credit       REAL DEFAULT 0,        -- WB 专有计费单位（credit-usage 事件累计）
  source_path  TEXT,
  synced_at    INTEGER,
  PRIMARY KEY(agent, session_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events(
  id           INTEGER PRIMARY KEY,
  agent        TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  ts           INTEGER,
  kind         TEXT NOT NULL,
  tool_name    TEXT,
  text         TEXT,
  status       TEXT,
  redacted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ev_sess ON events(agent, session_id);
CREATE INDEX IF NOT EXISTS idx_ev_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_ev_tool ON events(tool_name);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text, content='events', content_rowid='id', tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS usage(
  agent TEXT, session_id TEXT, ts INTEGER, step INTEGER DEFAULT 0, model TEXT,
  in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0, cache_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, cost_cny REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_sess ON usage(agent, session_id);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);

CREATE TABLE IF NOT EXISTS sync_state(
  source_key TEXT PRIMARY KEY,
  last_mtime REAL,
  last_ts INTEGER
) WITHOUT ROWID;

-- 语义事件层：agent 沉淀的"结论"（决策/事实/产物/教训），带原文回溯指针
CREATE TABLE IF NOT EXISTS insights(
  id         INTEGER PRIMARY KEY,
  agent      TEXT NOT NULL,          -- 来源端（沉淀者所在端）
  session_id TEXT,                   -- 回溯指针：结论出自哪个会话
  project    TEXT,                   -- 逻辑项目名（可空）
  type       TEXT NOT NULL,          -- decision/fact/artifact/lesson
  text       TEXT NOT NULL,
  refs       TEXT,                   -- JSON 数组：文件路径/会话等关联物
  ts         INTEGER,                -- 结论对应事件时间
  created_at INTEGER,                -- 沉淀时间
  redacted   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ins_proj ON insights(project);
CREATE INDEX IF NOT EXISTS idx_ins_type ON insights(type);

-- 跨端项目别名映射：一个逻辑项目散落在各端的 project 名
-- （一个 alias 可属多个逻辑项目，如一个宽泛的工作区别名同时服务多个逻辑项目）
CREATE TABLE IF NOT EXISTS projects(
  logical TEXT NOT NULL,        -- 逻辑项目名（用户自定义，如 MyProject）
  agent   TEXT NOT NULL,        -- 端
  alias   TEXT NOT NULL,        -- 该端实际 project_name 值
  PRIMARY KEY (logical, agent, alias)
) WITHOUT ROWID;
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    con.executescript(SCHEMA)
    # 轻量列迁移（旧库补新列）
    cols = {r["name"] for r in con.execute("PRAGMA table_info(sessions)")}
    if "credit" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN credit REAL DEFAULT 0")
    ucols = {r["name"] for r in con.execute("PRAGMA table_info(usage)")}
    if "total_tokens" not in ucols:
        con.execute("ALTER TABLE usage ADD COLUMN total_tokens INTEGER DEFAULT 0")
    con.commit()
    return con


if __name__ == "__main__":
    con = init()
    n = con.execute("SELECT count(*) c FROM sqlite_master").fetchone()["c"]
    print(f"initialized {DB_PATH}  (objects={n})")
