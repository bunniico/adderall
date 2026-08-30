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
    "manual_order": False,     # you arrange the list yourself (set by dragging)
    "active_project": "",      # id of the project tab currently open
    "api_key": "",             # optional; falls back to ANTHROPIC_API_KEY env
    "workspace_id": "",        # required for identity-linked keys; falls back
                               # to ANTHROPIC_WORKSPACE_ID env
    "models": {
        "fast": "claude-haiku-4-5",    # estimates, impact/effort scoring
        "balanced": "claude-sonnet-5", # interactive task breakdown
        "deep": "claude-opus-5",       # braindump compiler / bulk work
    },
}

DEFAULT_PROJECT_NAME = "Tasks"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    order_index   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    parent_id     TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    project_id    TEXT REFERENCES projects(id) ON DELETE CASCADE,
    deadline      TEXT,
    estimated_time INTEGER,
    actual_time   INTEGER,
    impact        INTEGER,
    effort        INTEGER,
    status        TEXT NOT NULL DEFAULT 'todo',
    ack_thankless INTEGER NOT NULL DEFAULT 0,
    collapsed     INTEGER NOT NULL DEFAULT 0,
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
    "title", "description", "parent_id", "project_id", "deadline", "estimated_time",
    "actual_time", "impact", "effort", "status", "ack_thankless", "collapsed",
    "order_index", "started_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return secrets.token_urlsafe(6)


def init() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema.

    Databases written before projects existed have no `project_id` column and
    no projects at all. Adding the column and adopting every loose task into
    one default project keeps an upgrade a no-op from the user's side: their
    list is simply the first tab.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "collapsed" not in cols:
        # Older databases predate collapsible tasks; everything starts open,
        # which is exactly how the list looked before the column existed.
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN collapsed INTEGER NOT NULL DEFAULT 0"
        )
    if "project_id" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN project_id TEXT "
            "REFERENCES projects(id) ON DELETE CASCADE"
        )
    # Indexed here rather than in SCHEMA: on an older database the column
    # does not exist until the line above has run.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
    row = conn.execute(
        "SELECT id FROM projects ORDER BY order_index, created_at LIMIT 1"
    ).fetchone()
    if row is None:
        project_id = _insert_project(conn, DEFAULT_PROJECT_NAME)
    else:
        project_id = row["id"]
    # Tasks whose project was never set (pre-projects rows, or a row written
    # while the column was still nullable) belong to the first tab.
    conn.execute(
        "UPDATE tasks SET project_id = ? WHERE project_id IS NULL", (project_id,)
    )


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


# ---------- projects (the tabs) ----------

def _insert_project(conn: sqlite3.Connection, name: str) -> str:
    ts = now_iso()
    project_id = new_id()
    order_index = conn.execute(
        "SELECT COALESCE(MAX(order_index), -1) + 1 FROM projects"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO projects (id, name, order_index, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, name, order_index, ts, ts),
    )
    return project_id


def list_projects() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY order_index, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def create_project(name: str) -> dict:
    with connect() as conn:
        project_id = _insert_project(conn, name)
    return get_project(project_id)


def rename_project(project_id: str, name: str) -> dict | None:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
            (name, now_iso(), project_id),
        )
        if cur.rowcount == 0:
            return None
    return get_project(project_id)


def delete_project(project_id: str) -> bool:
    """Delete a project and, by cascade, every task inside it."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0


def ensure_project() -> dict:
    """The first project, created if none exists. There is always a tab."""
    projects = list_projects()
    if projects:
        return projects[0]
    return create_project(DEFAULT_PROJECT_NAME)


def open_task_counts() -> dict[str, int]:
    """Unfinished tasks per project — the number on each tab."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM tasks "
            "WHERE status IN ('todo', 'in_progress') GROUP BY project_id"
        ).fetchall()
    return {r["project_id"]: r["n"] for r in rows}


def _row_to_task(row: sqlite3.Row) -> dict:
    task = dict(row)
    task["ack_thankless"] = bool(task["ack_thankless"])
    task["collapsed"] = bool(task["collapsed"])
    return task


def list_tasks(project_id: str | None = None) -> list[dict]:
    """Every task, or just the ones in one project (what a tab shows)."""
    sql = "SELECT * FROM tasks"
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params = (project_id,)
    sql += " ORDER BY order_index, created_at"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
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
                "SELECT COALESCE(MAX(order_index), -1) + 1 FROM tasks "
                "WHERE parent_id IS ? AND project_id IS ?",
                (fields.get("parent_id"), fields.get("project_id")),
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


def sibling_ids(parent_id: str | None, project_id: str,
                exclude: str | None = None) -> list[str]:
    """Ids under `parent_id` in `project_id`, in their current manual order.

    Top-level tasks of different projects all have a null parent, so the
    project is part of what "sibling" means — without it the tabs would
    renumber each other.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE parent_id IS ? AND project_id IS ? "
            "ORDER BY order_index, created_at",
            (parent_id, project_id),
        ).fetchall()
    return [r["id"] for r in rows if r["id"] != exclude]


def reorder_siblings(parent_id: str | None, project_id: str,
                     ordered_ids: list[str]) -> None:
    """Write `ordered_ids` back as the order_index sequence under `parent_id`.

    Ids not in the list keep their relative order and follow behind, so a
    partial list (say, only the tasks currently on screen) is safe to pass.
    """
    tail = [tid for tid in sibling_ids(parent_id, project_id)
            if tid not in ordered_ids]
    with connect() as conn:
        for idx, tid in enumerate([*ordered_ids, *tail]):
            conn.execute("UPDATE tasks SET order_index = ? WHERE id = ?", (idx, tid))


def move_task(task_id: str, parent_id: str | None, project_id: str,
              position: int | None = None) -> dict | None:
    """Reparent and/or reposition one task, renumbering its new siblings.

    `position` is the index among the target siblings *after* the task has
    been lifted out of wherever it was; None appends. Order indices are
    rewritten as a dense 0..n-1 sequence so later inserts stay unambiguous.
    """
    siblings = sibling_ids(parent_id, project_id, exclude=task_id)
    if position is None:
        position = len(siblings)
    position = max(0, min(position, len(siblings)))
    siblings.insert(position, task_id)
    ts = now_iso()
    with connect() as conn:
        for idx, tid in enumerate(siblings):
            if tid == task_id:
                conn.execute(
                    "UPDATE tasks SET parent_id = ?, order_index = ?, updated_at = ? "
                    "WHERE id = ?",
                    (parent_id, idx, ts, tid),
                )
            else:
                conn.execute("UPDATE tasks SET order_index = ? WHERE id = ?", (idx, tid))
    return get_task(task_id)


def move_task_to_project(task_id: str, project_id: str) -> dict | None:
    """Move a task — and everything nested under it — into another project.

    The task lands at the top level of its new project: its old parent lives
    in a different tab, so the nesting it had there cannot survive the move.
    """
    ids = [task_id, *descendant_ids(task_id)]
    ts = now_iso()
    with connect() as conn:
        tail = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM tasks "
            "WHERE parent_id IS NULL AND project_id IS ?",
            (project_id,),
        ).fetchone()[0]
        for tid in ids:
            conn.execute(
                "UPDATE tasks SET project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, ts, tid),
            )
        conn.execute(
            "UPDATE tasks SET parent_id = NULL, order_index = ? WHERE id = ?",
            (tail, task_id),
        )
    return get_task(task_id)


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
