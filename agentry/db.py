"""SQLite storage. One connection shared across threads, guarded by a lock."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,          -- 'admin' for the local account, '<iss>|<sub>' for SSO
  email TEXT, name TEXT,
  role TEXT NOT NULL DEFAULT 'viewer',    -- admin | reviewer | viewer | none
  local INTEGER NOT NULL DEFAULT 0,
  password_hash TEXT,
  created_at REAL NOT NULL, last_login REAL
);
CREATE TABLE IF NOT EXISTS model_servers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- ollama | openai
  base_url TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '',       -- encrypted
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- sdp
  config TEXT NOT NULL DEFAULT '{}',      -- non-secret settings (JSON)
  secret TEXT NOT NULL DEFAULT '',        -- encrypted
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  schedule_minutes INTEGER NOT NULL DEFAULT 0,   -- 0 = manual only
  config TEXT NOT NULL DEFAULT '{}',
  next_run_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_state (
  agent_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (agent_id, key)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  trigger TEXT NOT NULL,                  -- schedule | manual
  triggered_by TEXT,
  status TEXT NOT NULL,                   -- running | ok | error | cancelled
  started_at REAL NOT NULL, finished_at REAL,
  summary TEXT
);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent_id, started_at DESC);
CREATE TABLE IF NOT EXISTS run_logs (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL, at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_logs_run ON run_logs(run_id, id);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  run_id INTEGER,
  kind TEXT NOT NULL,                     -- update | create
  target_id TEXT,                         -- solution id for updates
  title TEXT NOT NULL,
  rationale TEXT,
  original TEXT,                          -- JSON snapshot of the record before the change
  proposed TEXT NOT NULL,                 -- JSON of the new fields
  status TEXT NOT NULL DEFAULT 'pending', -- pending | applied | rejected | failed
  decided_by TEXT, decided_at REAL, result TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status, created_at DESC);
CREATE TABLE IF NOT EXISTS embeddings (
  key TEXT NOT NULL, model TEXT NOT NULL, hash TEXT NOT NULL, vector TEXT NOT NULL,
  PRIMARY KEY (key, model)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, user TEXT, action TEXT NOT NULL, detail TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    if path is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = str(settings.data_dir / "agentry.db")
    _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    # Runs left 'running' by a crash or restart never finish on their own.
    _conn.execute("UPDATE runs SET status='error', finished_at=?, summary='Interrupted (Agentry restarted)' "
                  "WHERE status='running'", (time.time(),))
    return _conn


def conn() -> sqlite3.Connection:
    return _conn or connect()


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args: tuple = ()) -> int:
    """Runs a write and returns lastrowid."""
    with _lock:
        return conn().execute(sql, args).lastrowid


def audit(user: str | None, action: str, detail: Any = None) -> None:
    ex("INSERT INTO audit(at, user, action, detail) VALUES (?,?,?,?)",
       (time.time(), user, action, json.dumps(detail) if detail is not None else None))


def state_get(agent_id: int, key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM agent_state WHERE agent_id=? AND key=?", (agent_id, key))
    return json.loads(row["value"]) if row else default


def state_set(agent_id: int, key: str, value: Any) -> None:
    ex("INSERT INTO agent_state(agent_id, key, value) VALUES (?,?,?) "
       "ON CONFLICT(agent_id, key) DO UPDATE SET value=excluded.value", (agent_id, key, json.dumps(value)))
