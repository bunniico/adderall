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
    collapsed: bool | None = None
    order_index: int | None = None


class TaskMove(BaseModel):
    """Where a task should land.

    Either absolute (`parent_id` + `position`) or relative to another task
    (`target_id` + `mode`). Relative is what the front end sends: it says
    "put this one before/after/inside that one" and lets the server work out
    the index, so a drop always means what it looked like on screen.
    """
    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0)
    target_id: str | None = None
    mode: str | None = Field(default=None, pattern="^(before|after|into)$")


class ProjectCreate(BaseModel):
    name: str = Field(default="New project", min_length=1, max_length=80)


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TaskProjectMove(BaseModel):
    project_id: str


class BreakdownRequest(BaseModel):
    granularity: int | None = Field(default=None, ge=1, le=5)


class CompleteRequest(BaseModel):
    actual_time: int | None = Field(default=None, ge=0)


class CompileRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class Nudge(BaseModel):
    """One task's new deadline, as an ISO instant the page worked out locally.

    Day boundaries, "tomorrow" and "tonight" are all local-timezone ideas and
    the server has no timezone, so the page computes the instant and this
    just records it.
    """
    task_id: str
    deadline: str


class NudgeRequest(BaseModel):
    nudges: list[Nudge] = Field(min_length=1, max_length=500)


class SettingsUpdate(BaseModel):
    model_config = {"extra": "allow"}


# ---------- helpers ----------

def _tree(tasks: list[dict], derived: dict[str, dict]) -> list[dict]:
    """Nest a flat task list into roots + subtasks, derived fields merged in."""
    by_id: dict[str, dict] = {}
    for t in tasks:
        by_id[t["id"]] = {**t, **derived[t["id"]], "subtasks": []}
    roots = []
    for t in tasks:
        node = by_id[t["id"]]
        if t["parent_id"] and t["parent_id"] in by_id:
            by_id[t["parent_id"]]["subtasks"].append(node)
        else:
            roots.append(node)
    return roots


def _active_project_id_raw() -> str:
    """Whatever id is stored, without resolving it against existing projects."""
    return (db.get_settings().get("active_project") or "").strip()


def _active_project_id(projects: list[dict]) -> str:
    """The project tab the page is on, remembered across reloads.

    Falls back to the first tab whenever the stored one is gone (deleted on
    another device, or a fresh database), and writes the fallback back so the
    answer stays stable.
    """
    stored = _active_project_id_raw()
    if any(p["id"] == stored for p in projects):
        return stored
    db.update_settings({"active_project": projects[0]["id"]})
    return projects[0]["id"]


def _state(project_id: str | None = None) -> dict:
    """Everything the page renders: the open tab's task tree, the tab strip,
    and the cross-project deadline list the alarms run off.

    Only the active project's tasks are sent as a tree — a tab you are not
    looking at is not on screen — but deadlines are gathered from every
    project, because a transition alarm you miss because its task lives in
    another tab is exactly the failure this app exists to prevent.
    """
    settings = db.get_settings()
    ratios = db.completion_ratios()
    projects = db.list_projects() or [db.ensure_project()]
    active_id = project_id or _active_project_id(projects)

    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)

    counts = db.open_task_counts()
    alarm_tasks: list[dict] = []
    tree: list[dict] = []
    next_task_id = None
    for project in projects:
        tasks = by_project.get(project["id"], [])
        derived = logic.compute(tasks, settings, ratios)
        for t in tasks:
            d = derived[t["id"]]
            if t["status"] in logic.ACTIVE_STATUSES and d.get("deadline"):
                alarm_tasks.append({
                    "id": t["id"], "title": t["title"], "deadline": d["deadline"],
                    "project_id": project["id"], "project_name": project["name"],
                })
        if project["id"] == active_id:
            tree = _tree(tasks, derived)
            nxt = logic.next_task(tasks, derived)
            next_task_id = nxt["id"] if nxt else None

    return {
        "tasks": tree,
        "next_task_id": next_task_id,
        "projects": [{**p, "open_tasks": counts.get(p["id"], 0)} for p in projects],
        "active_project_id": active_id,
        "alarm_tasks": alarm_tasks,
    }


def _calendar_events() -> list[dict]:
    """Every scheduled task across every project, with what a calendar needs.

    The calendar is deliberately the one place in the app that ignores the
    open tab: "what is due this week" is a question about your whole life,
    not about whichever list happens to be on screen. Filtering by project
    happens on the page, over this one payload, so switching filters or
    flipping between day, week and month costs nothing.

    Times go out as UTC instants. Which day a task lands on, where a block
    sits in a day, and what "this week" means are all local-timezone
    questions, and the page is the only side that knows the timezone.
    """
    settings = db.get_settings()
    ratios = db.completion_ratios()
    projects = db.list_projects() or [db.ensure_project()]

    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)

    events: list[dict] = []
    for project in projects:
        tasks = by_project.get(project["id"], [])
        derived = logic.compute(tasks, settings, ratios)
        children_count: dict[str, int] = {}
        for t in tasks:
            if t["parent_id"] and t["status"] != "discarded":
                children_count[t["parent_id"]] = children_count.get(t["parent_id"], 0) + 1
        for t in tasks:
            d = derived[t["id"]]
            # Discarded work is off the plan entirely; done work stays, because
            # a calendar you can look back at is half of what a calendar is for.
            if t["status"] == "discarded" or not d.get("deadline"):
                continue
            length = _block_length(d)
            buf = d["buffer_applied"] or 0.0
            events.append({
                "id": t["id"],
                "title": t["title"],
                "description": t["description"],
                "parent_id": t["parent_id"],
                "project_id": project["id"],
                "project_name": project["name"],
                "status": t["status"],
                "deadline": d["deadline"],
                "deadline_source": d["deadline_source"],
                "estimated_time": t["estimated_time"],
                "buffered_estimate": d["buffered_estimate"],
                "buffer_applied": buf,
                # How long the block is, and how much of that is the time tax
                # rather than the work — the day view draws the difference.
                "length_min": length,
                "raw_length_min": max(1, round(length / (1.0 + buf))),
                "impact": t["impact"],
                "effort": t["effort"],
                "quadrant": d["quadrant"],
                "urgency": d["urgency"],
                "score": d["score"],
                "has_subtasks": d["has_subtasks"],
                "subtask_count": children_count.get(t["id"], 0),
                "path": logic.ancestor_titles(tasks, t),
            })
    return events


def _block_length(derived: dict) -> int:
    """How much time a task takes up on the calendar, in minutes.

    A container is worth the work it still holds, a leaf its own buffered
    estimate, and a task nobody has estimated yet gets the same default the
    urgency maths uses rather than a zero-height sliver.
    """
    if derived["has_subtasks"]:
        length = (derived["rollup_remaining"] or derived["rollup_estimate"]
                  or derived["buffered_estimate"])
    else:
        length = derived["buffered_estimate"]
    return max(1, int(length or logic.DEFAULT_ESTIMATE_MIN))


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


def _reveal(task_id: str | None) -> None:
    """Unfold a collapsed task, so whatever just landed inside it is on screen.

    Adding a subtask to a folded-away task and seeing nothing happen is
    exactly the "did that even work?" moment this app exists to spare you,
    so anything that puts a task inside another one opens the container.
    """
    if not task_id:
        return
    task = db.get_task(task_id)
    if task and task["collapsed"]:
        db.update_task(task_id, {"collapsed": False})


def _require_task(task_id: str) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


def _require_project(project_id: str) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _freeze_manual_order() -> None:
    """Switch the list from auto-sort to hand-arranged, keeping what's on screen.

    Top-level tasks are normally shown in urgency order rather than in stored
    order, so the first drag writes the order you were looking at into
    `order_index` before moving anything. Without that, everything else would
    jump the moment manual order took effect.
    """
    settings = db.get_settings()
    if settings.get("manual_order"):
        return
    ratios = db.completion_ratios()
    # Manual order is one switch for the whole app, so every tab's top level
    # is frozen, not just the one being dragged in — otherwise the other tabs
    # would silently rearrange the next time you looked at them.
    for project in db.list_projects():
        tasks = db.list_tasks(project["id"])
        derived = logic.compute(tasks, settings, ratios)
        roots = [t for t in tasks if t["parent_id"] is None]
        roots.sort(key=lambda t: derived[t["id"]]["sort_key"])
        db.reorder_siblings(None, project["id"], [t["id"] for t in roots])
    db.update_settings({"manual_order": True})


# ---------- routes ----------

@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/api/state")
def get_state():
    return _state()


# ---------- projects ----------
# Tabs across the top: one list of tasks each, one open at a time. Every
# project route answers with the full page state, so switching tabs, renaming
# one, or deleting one is a single round trip like every other mutation.

@app.get("/api/projects")
def list_projects():
    state = _state()
    return {"projects": state["projects"],
            "active_project_id": state["active_project_id"]}


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate):
    """Add a tab and switch to it — a new project is always one you want to
    start filling in immediately."""
    project = db.create_project(body.name.strip() or "New project")
    db.update_settings({"active_project": project["id"]})
    return _state(project["id"])


@app.patch("/api/projects/{project_id}")
def rename_project(project_id: str, body: ProjectUpdate):
    _require_project(project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "A project needs a name")
    db.rename_project(project_id, name)
    return _state()


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    """Delete a project and everything in it. The last tab cannot go: there
    is always somewhere for a task to land."""
    projects = db.list_projects()
    if not any(p["id"] == project_id for p in projects):
        raise HTTPException(404, "Project not found")
    if len(projects) <= 1:
        raise HTTPException(400, "This is your only project — rename it instead")
    index = next(i for i, p in enumerate(projects) if p["id"] == project_id)
    neighbour = projects[index - 1] if index else projects[1]
    db.delete_project(project_id)
    if _active_project_id_raw() == project_id:
        db.update_settings({"active_project": neighbour["id"]})
    return _state()


@app.post("/api/projects/{project_id}/activate")
def activate_project(project_id: str):
    _require_project(project_id)
    db.update_settings({"active_project": project_id})
    return _state(project_id)


@app.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate):
    project_id = _active_project_id(db.list_projects() or [db.ensure_project()])
    if body.parent_id:
        parent = _require_task(body.parent_id)
        project_id = parent["project_id"]  # a subtask lives where its parent does
    fields = body.model_dump(exclude={"annotate"})
    fields["project_id"] = project_id
    task = db.create_task(fields)
    _reveal(body.parent_id)
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


@app.post("/api/tasks/{task_id}/move")
def move_task(task_id: str, body: TaskMove):
    """Reorder or renest one task. Turns manual ordering on the first time.

    A move stays inside one project — dragging is for arranging a list, not
    for crossing tabs; `POST /api/tasks/{id}/project` does that.
    """
    task = _require_task(task_id)
    project_id = task["project_id"]

    parent_id = body.parent_id
    target = None
    if body.target_id:
        if not body.mode:
            raise HTTPException(400, "mode is required when target_id is given")
        if body.target_id == task_id:
            raise HTTPException(400, "A task cannot be dropped onto itself")
        target = _require_task(body.target_id)
        if target["project_id"] != project_id:
            raise HTTPException(400, "That task is in a different project")
        parent_id = target["id"] if body.mode == "into" else target["parent_id"]

    if parent_id:
        if parent_id == task_id:
            raise HTTPException(400, "A task cannot be its own parent")
        parent = _require_task(parent_id)
        if parent["project_id"] != project_id:
            raise HTTPException(400, "That task is in a different project")
        if parent_id in db.descendant_ids(task_id):
            raise HTTPException(400, "A task cannot be moved inside its own subtasks")

    _freeze_manual_order()

    position = body.position
    if target is not None and body.mode != "into":
        siblings = db.sibling_ids(parent_id, project_id, exclude=task_id)
        idx = siblings.index(target["id"])
        position = idx if body.mode == "before" else idx + 1
    elif target is not None:
        position = None  # dropped onto a task: land at the end of its subtasks

    db.move_task(task_id, parent_id, project_id, position)
    _reveal(parent_id)
    return _state()


@app.post("/api/tasks/{task_id}/project")
def move_task_to_project(task_id: str, body: TaskProjectMove):
    """Send a task (with its subtasks) to another tab."""
    task = _require_task(task_id)
    _require_project(body.project_id)
    if task["project_id"] != body.project_id:
        db.move_task_to_project(task_id, body.project_id)
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
        sub = db.create_task({"title": step, "parent_id": task_id,
                              "project_id": task["project_id"]})
        new_ids.append(sub["id"])
    if new_ids:
        _reveal(task_id)
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
    project_id = _active_project_id(db.list_projects() or [db.ensure_project()])
    new_ids = []
    for item in items:
        task = db.create_task({
            "title": item["title"].strip(),
            "description": item.get("description", "").strip(),
            "project_id": project_id,
        })
        new_ids.append(task["id"])
    _annotate_tasks(new_ids, want_scores=settings["ai_scoring"])
    return _state()


@app.get("/api/next")
def get_next():
    project_id = _active_project_id(db.list_projects() or [db.ensure_project()])
    tasks = db.list_tasks(project_id)
    settings = db.get_settings()
    derived = logic.compute(tasks, settings, db.completion_ratios())
    nxt = logic.next_task(tasks, derived)
    if not nxt:
        return {"task": None}
    return {"task": {**nxt, **derived[nxt["id"]]}}


@app.get("/api/focus")
def get_focus(root: str | None = None):
    """The ordered walk Taskmaster follows through a task tree.

    `root` scopes the session to one task's subtree; without it the most
    urgent tree in the open tab is chosen. The queue is depth-first —
    subtasks before the task that contains them — so a session drills down
    to the smallest first step and works its way back up.

    A named root keeps the session in *its* project, whichever tab is open:
    ducking out to another project to jot something down must not hijack a
    running timer onto a different task.
    """
    if root:
        project_id = _require_task(root)["project_id"]
    else:
        project_id = _active_project_id(db.list_projects() or [db.ensure_project()])
    tasks = db.list_tasks(project_id)
    settings = db.get_settings()
    derived = logic.compute(tasks, settings, db.completion_ratios())
    root_id = root or logic.focus_root_id(tasks, derived)
    if not root_id:
        return {"root_id": None, "root_title": None, "project_id": project_id,
                "queue": []}
    by_id = {t["id"]: t for t in tasks}
    if root_id not in by_id:
        raise HTTPException(404, "Task not found")
    queue = [
        {**t, **derived[t["id"]], "path": logic.ancestor_titles(tasks, t)}
        for t in logic.focus_queue(tasks, root_id)
    ]
    return {
        "root_id": root_id,
        "root_title": by_id[root_id]["title"],
        "project_id": project_id,
        "queue": queue,
    }


@app.get("/api/calendar")
def get_calendar():
    """Everything with a date on it, from every project, scored.

    One payload for all three views: the page slices it into days, weeks and
    months locally, so ‹ › navigation and changing a filter never wait on the
    network.
    """
    return {"events": _calendar_events()}


@app.post("/api/nudge")
def nudge(body: NudgeRequest):
    """Push past-due tasks onto new deadlines, keeping the shape of each plan.

    A deadline that has already gone by is the most demoralizing thing a list
    like this can show you, and re-typing a date for every item is exactly the
    friction that leaves it showing. One call moves one task or the whole
    overdue pile; each task keeps its length (its estimate never changes, so
    the block is the same size on the new day), and anything nested under it
    with a deadline of its own slides by the same amount rather than piling up
    on the new date.
    """
    settings = db.get_settings()
    ratios = db.completion_ratios()

    wanted: dict[str, datetime] = {}
    for item in body.nudges:
        when = logic.parse_dt(item.deadline)
        if when is None:
            raise HTTPException(400, f"Unparseable deadline: {item.deadline!r}")
        _require_task(item.task_id)
        wanted[item.task_id] = when

    # Group by project so each task's current (possibly auto-assigned) deadline
    # — the thing the shift is measured from — is computed the same way the
    # list computes it.
    by_project: dict[str, list[dict]] = {}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)

    moves: dict[str, str] = {}
    for task_id, when in wanted.items():
        task = db.get_task(task_id)
        tasks = by_project.get(task["project_id"], [])
        derived = logic.compute(tasks, settings, ratios)
        # Later entries win, so nudging a parent and one of its children in the
        # same call lands the child where it was asked to go.
        for tid, iso in logic.nudge_plan(tasks, derived, task_id, when).items():
            moves.setdefault(tid, iso)
        moves[task_id] = when.isoformat(timespec="seconds")

    for tid, iso in moves.items():
        db.update_task(tid, {"deadline": iso})
    return _state()


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
