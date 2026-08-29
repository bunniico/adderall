"""Adderall — a single-page, single-user executive dysfunction workspace.

One FastAPI process serves both the JSON API and the static front end.
All state lives in a local SQLite file; every mutation persists immediately.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, db, logic

app = FastAPI(title="Adderall", docs_url="/api/docs", openapi_url="/api/openapi.json")

db.init()


# ---------- request bodies ----------

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    parent_id: str | None = None
    deadline: str | None = None
    estimated_time: int | None = Field(default=None, ge=1)
    impact: int | None = Field(default=None, ge=0, le=10)
    effort: int | None = Field(default=None, ge=0, le=10)
    annotate: bool = True  # ask the AI for estimate/scores when missing


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    deadline: str | None = None
    clear_deadline: bool = False
    estimated_time: int | None = Field(default=None, ge=1)
    impact: int | None = Field(default=None, ge=0, le=10)
    effort: int | None = Field(default=None, ge=0, le=10)
    status: str | None = Field(default=None, pattern="^(todo|in_progress|done|discarded)$")
    ack_thankless: bool | None = None
    order_index: int | None = None


class BreakdownRequest(BaseModel):
    granularity: int | None = Field(default=None, ge=1, le=5)


class CompleteRequest(BaseModel):
    actual_time: int | None = Field(default=None, ge=0)


class CompileRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class SettingsUpdate(BaseModel):
    model_config = {"extra": "allow"}


# ---------- helpers ----------

def _state() -> dict:
    """Full task tree with derived fields — the one payload the page renders."""
    tasks = db.list_tasks()
    settings = db.get_settings()
    derived = logic.compute(tasks, settings, db.completion_ratios())
    by_id: dict[str, dict] = {}
    for t in tasks:
        merged = {**t, **derived[t["id"]], "subtasks": []}
        by_id[t["id"]] = merged
    roots = []
    for t in tasks:
        node = by_id[t["id"]]
        if t["parent_id"] and t["parent_id"] in by_id:
            by_id[t["parent_id"]]["subtasks"].append(node)
        else:
            roots.append(node)
    nxt = logic.next_task(tasks, derived)
    return {"tasks": roots, "next_task_id": nxt["id"] if nxt else None}


def _annotate_tasks(task_ids: list[str], want_scores: bool) -> None:
    """Best effort: fill missing estimates/scores via one batched fast-tier
    call. Never blocks or fails the surrounding operation."""
    settings = db.get_settings()
    targets = []
    for tid in task_ids:
        t = db.get_task(tid)
        if t and (t["estimated_time"] is None or
                  (want_scores and (t["impact"] is None or t["effort"] is None))):
            targets.append(t)
    if not targets:
        return
    try:
        results = ai.annotate(settings, targets, want_scores=want_scores)
    except (ai.AIUnavailable, Exception):
        return
    for t in targets:
        row = results.get(t["id"])
        if not row:
            continue
        fields = {}
        if t["estimated_time"] is None and row.get("minutes"):
            fields["estimated_time"] = int(row["minutes"])
        if want_scores and t["impact"] is None and row.get("impact") is not None:
            fields["impact"] = int(row["impact"])
        if want_scores and t["effort"] is None and row.get("effort") is not None:
            fields["effort"] = int(row["effort"])
        if fields:
            db.update_task(t["id"], fields)


def _require_task(task_id: str) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


# ---------- routes ----------

@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/api/state")
def get_state():
    return _state()


@app.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate):
    if body.parent_id:
        _require_task(body.parent_id)
    fields = body.model_dump(exclude={"annotate"})
    task = db.create_task(fields)
    settings = db.get_settings()
    if body.annotate:
        _annotate_tasks([task["id"]], want_scores=settings["ai_scoring"])
    return _state()


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: str, body: TaskUpdate):
    _require_task(task_id)
    fields = body.model_dump(exclude_unset=True, exclude={"clear_deadline"})
    if body.clear_deadline:
        fields["deadline"] = None
    if fields.get("status") == "in_progress":
        fields["started_at"] = db.now_iso()
    db.update_task(task_id, fields)
    if fields.get("status") == "done":
        for tid in db.descendant_ids(task_id):
            child = db.get_task(tid)
            if child and child["status"] in ("todo", "in_progress"):
                db.update_task(tid, {"status": "done"})
    return _state()


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    if not db.delete_task(task_id):
        raise HTTPException(404, "Task not found")
    return _state()


@app.post("/api/tasks/{task_id}/breakdown")
def breakdown_task(task_id: str, body: BreakdownRequest):
    task = _require_task(task_id)
    settings = db.get_settings()
    granularity = body.granularity or settings["granularity"]
    parents = []
    cursor = task
    while cursor.get("parent_id"):
        cursor = db.get_task(cursor["parent_id"])
        if not cursor:
            break
        parents.insert(0, cursor["title"])
    try:
        steps = ai.breakdown(settings, task["title"], task["description"],
                             granularity, parents)
    except ai.AIUnavailable as exc:
        raise HTTPException(502, str(exc))
    new_ids = []
    for step in steps:
        sub = db.create_task({"title": step, "parent_id": task_id})
        new_ids.append(sub["id"])
    _annotate_tasks(new_ids, want_scores=settings["ai_scoring"])
    return _state()


@app.post("/api/tasks/{task_id}/annotate")
def annotate_task(task_id: str):
    task = _require_task(task_id)
    settings = db.get_settings()
    # Explicit re-annotation clears prior AI values so they refresh.
    try:
        results = ai.annotate(settings, [task], want_scores=settings["ai_scoring"])
    except ai.AIUnavailable as exc:
        raise HTTPException(502, str(exc))
    row = results.get(task_id)
    if row:
        fields = {"estimated_time": int(row["minutes"])}
        if settings["ai_scoring"]:
            if row.get("impact") is not None:
                fields["impact"] = int(row["impact"])
            if row.get("effort") is not None:
                fields["effort"] = int(row["effort"])
        db.update_task(task_id, fields)
    return _state()


@app.post("/api/tasks/{task_id}/start")
def start_task(task_id: str):
    _require_task(task_id)
    db.update_task(task_id, {"status": "in_progress", "started_at": db.now_iso()})
    return _state()


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, body: CompleteRequest):
    task = _require_task(task_id)
    actual = body.actual_time
    if actual is None and task["started_at"]:
        started = logic.parse_dt(task["started_at"])
        if started:
            actual = max(1, round((datetime.now(timezone.utc) - started).total_seconds() / 60))
    fields: dict = {"status": "done"}
    if actual is not None:
        fields["actual_time"] = actual
    db.update_task(task_id, fields)
    for tid in db.descendant_ids(task_id):
        child = db.get_task(tid)
        if child and child["status"] in ("todo", "in_progress"):
            db.update_task(tid, {"status": "done"})
    return _state()


@app.post("/api/compile")
def compile_braindump(body: CompileRequest):
    settings = db.get_settings()
    try:
        items = ai.compile_braindump(settings, body.text)
    except ai.AIUnavailable as exc:
        raise HTTPException(502, str(exc))
    new_ids = []
    for item in items:
        task = db.create_task({
            "title": item["title"].strip(),
            "description": item.get("description", "").strip(),
        })
        new_ids.append(task["id"])
    _annotate_tasks(new_ids, want_scores=settings["ai_scoring"])
    return _state()


@app.get("/api/next")
def get_next():
    tasks = db.list_tasks()
    settings = db.get_settings()
    derived = logic.compute(tasks, settings, db.completion_ratios())
    nxt = logic.next_task(tasks, derived)
    if not nxt:
        return {"task": None}
    return {"task": {**nxt, **derived[nxt["id"]]}}


@app.get("/api/settings")
def get_settings():
    settings = db.get_settings()
    # Never echo the key back to the page; report only whether one exists.
    has_key = bool((settings.pop("api_key", "") or "").strip()
                   or os.environ.get("ANTHROPIC_API_KEY"))
    settings["has_api_key"] = has_key
    return settings


@app.put("/api/settings")
def put_settings(body: SettingsUpdate):
    changes = body.model_dump()
    changes.pop("has_api_key", None)
    if changes.get("api_key") == "":
        changes.pop("api_key")  # empty field means "leave unchanged"
    db.update_settings(changes)
    return get_settings()


static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
