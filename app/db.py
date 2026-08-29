"""SQLite persistence layer.

Reliability first: one local database file, WAL journaling, synchronous=FULL,
every write committed immediately. There is exactly one user and one writer,
so the whole class of sync/merge bugs cannot occur.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("ADDERALL_DB", os.path.join("data", "adderall.db"))

DEFAULT_SETTINGS = {
    "auto_deadlines": True,
    "buffer": 0.30,            # time-tax buffer, fraction of raw estimate
    "adaptive_buffer": False,  # learn from actual-vs-estimated history
    "matrix_threshold": 5,     # 0-10 split between low/high impact & effort
    "ai_scoring": True,        # let the AI seed impact/effort scores
    "alarms": {
        "enabled": True,
        "stop_lead": 30,       # minutes before deadline: "stop current activity"
        "ready_lead": 10,      # minutes before deadline: "get ready"
        "go_lead": 0,          # minutes before deadline: "time to start/leave"
    },
    "timer_style": "both",     # analog | block | both
    "granularity": 3,          # default breakdown spiciness 1-5
    "gamification": True,
    "api_key": "",             # optional; falls back to ANTHROPIC_API_KEY env
    "models": {
        "fast": "claude-haiku-4-5",    # estimates, impact/effort scoring
        "balanced": "claude-sonnet-5", # interactive task breakdown
        "deep": "claude-opus-5",       # braindump compiler / bulk work
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    parent_id     TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    deadline      TEXT,
    estimated_time INTEGER,
    actual_time   INTEGER,
    impact        INTEGER,
    effort        INTEGER,
    status        TEXT NOT NULL DEFAULT 'todo',
    ack_thankless INTEGER NOT NULL DEFAULT 0,
    order_index   INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

TASK_FIELDS = {
    "title", "description", "parent_id", "deadline", "estimated_time",
    "actual_time", "impact", "effort", "status", "ack_thankless",
    "order_index", "started_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return secrets.token_urlsafe(6)


def init() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> dict:
    task = dict(row)
    task["ack_thankless"] = bool(task["ack_thankless"])
    return task


def list_tasks() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY order_index, created_at"
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def create_task(fields: dict) -> dict:
    task_id = new_id()
    ts = now_iso()
    with connect() as conn:
        if "order_index" not in fields or fields["order_index"] is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(order_index), -1) + 1 FROM tasks WHERE parent_id IS ?",
                (fields.get("parent_id"),),
            ).fetchone()
            fields["order_index"] = row[0]
        cols = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        cols.update({"id": task_id, "created_at": ts, "updated_at": ts})
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO tasks ({names}) VALUES ({marks})", list(cols.values()))
    return get_task(task_id)


def update_task(task_id: str, fields: dict) -> dict | None:
    cols = {k: v for k, v in fields.items() if k in TASK_FIELDS}
    if not cols:
        return get_task(task_id)
    cols["updated_at"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in cols)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {sets} WHERE id = ?", [*cols.values(), task_id]
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def delete_task(task_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


def descendant_ids(task_id: str) -> list[str]:
    tasks = list_tasks()
    children: dict[str | None, list[str]] = {}
    for t in tasks:
        children.setdefault(t["parent_id"], []).append(t["id"])
    result: list[str] = []
    stack = list(children.get(task_id, []))
    while stack:
        tid = stack.pop()
        result.append(tid)
        stack.extend(children.get(tid, []))
    return result


def completion_ratios(limit: int = 20) -> list[float]:
    """actual/estimated ratios of recently completed tasks, for adaptive buffering."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT actual_time, estimated_time FROM tasks
               WHERE status = 'done' AND actual_time IS NOT NULL
                 AND estimated_time IS NOT NULL AND estimated_time > 0
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [r["actual_time"] / r["estimated_time"] for r in rows]


def get_settings() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT k, v FROM settings").fetchall()
    stored = {r["k"]: json.loads(r["v"]) for r in rows}
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    for key, value in stored.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def update_settings(changes: dict) -> dict:
    current = get_settings()
    with connect() as conn:
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if isinstance(DEFAULT_SETTINGS[key], dict) and isinstance(value, dict):
                merged = {**current[key], **value}
                value = {k: v for k, v in merged.items() if k in DEFAULT_SETTINGS[key] or key == "models"}
            conn.execute(
                "INSERT INTO settings (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, json.dumps(value)),
            )
    return get_settings()
