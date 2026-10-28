"""Adderall — a single-page, single-user executive dysfunction workspace.

One FastAPI process serves both the JSON API and the static front end.
All state lives in a local SQLite file; every mutation persists immediately.

The core routes (projects, tasks, planning, settings) are here. Each module's
routes are in `routes/`, one file per module, and can be switched off (see
`modules.py`); what both share is in `core.py`.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import (ai, clickup, core, db, events, logic, mcp_server, modules, queue,
               recurring, scheduler, title_parse)
from .routes import (alarms, braindump, calendar, estimates, focus, habits, help,
                     magic_todo, overview)
from .routes import clickup as clickup_routes
from .routes import recurring as recurring_routes

def _configure_logging() -> None:
    """Send the app's own logs to stdout, where `docker logs` reads them.

    uvicorn only configures its own loggers, so without a handler on the root
    logger everything this app logs — the AI exchanges in `ai.py` above all —
    would be dropped before it ever reached the container's output.
    basicConfig() is a no-op if something upstream has already configured
    logging, which is the right deference: an explicit setup wins.
    """
    level = getattr(logging, os.environ.get("ADDERALL_LOG_LEVEL", "INFO").strip().upper(),
                    logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # The HTTP stack stays at INFO however loud the app gets: its debug output
    # includes request headers, and one of those headers is the API key.
    for noisy in ("anthropic", "httpcore", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))


_configure_logging()

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the recurring-task sweep for as long as the app is up.

    Startup is the important half: a machine that was off over the weekend
    comes back to Monday's copies already on the list, rather than waiting for
    the next tick to notice. See `scheduler.py` for why this is a sweep on a
    timer instead of a job pinned to midnight.
    """
    scheduler.start(app)
    clickup.start(app)
    queue.start(app)
    events.start(app, lambda: core.state()["alarm_tasks"])
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await scheduler.stop(app)
        await clickup.stop(app)
        await queue.stop(app)
        await events.stop(app)


app = FastAPI(title="Adderall", docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)

db.init()


# ---------- request bodies ----------

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    parent_id: str | None = None
    deadline: str | None = None
    # When you would like to *begin*, as opposed to be finished by. Left
    # unset, the AI is asked for one (see `estimates.annotate_tasks`).
    start_at: str | None = None
    estimated_time: int | None = Field(default=None, ge=1)
    impact: int | None = Field(default=None, ge=0, le=10)
    effort: int | None = Field(default=None, ge=0, le=10)
    # How movable this is, 1 to 5. Left unset it is 3, which is how everything
    # behaved before there was a setting for it. See `logic.FLEXIBILITY`.
    flexibility: int | None = Field(default=None, ge=1, le=5)
    # "Keep this off my days off." Left unset it is on, because a day off you
    # have to defend task by task is not a day off. See `logic.task_days`.
    workday_only: bool | None = None
    annotate: bool = True  # ask the AI for estimate/scores/start when missing


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    deadline: str | None = None
    clear_deadline: bool = False
    start_at: str | None = None
    clear_start_at: bool = False
    estimated_time: int | None = Field(default=None, ge=1)
    impact: int | None = Field(default=None, ge=0, le=10)
    effort: int | None = Field(default=None, ge=0, le=10)
    status: str | None = Field(
        default=None, pattern="^(todo|in_progress|done|discarded|missed)$")
    flexibility: int | None = Field(default=None, ge=1, le=5)
    workday_only: bool | None = None
    ack_thankless: bool | None = None
    collapsed: bool | None = None
    order_index: int | None = None
    # Per-subtask: whether this step carries into the next occurrence of a
    # repeating task, or is a one-off added to just this one.
    repeat_carry: bool | None = None


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


class ProjectMove(BaseModel):
    """Where a dragged tab lands.

    Like a task move this is phrased as "put this tab before/after that one"
    rather than as an index, so a drop means what it looked like on screen.
    A bare `position` (or nothing at all) puts the tab at that index, or at
    the end of the strip.
    """
    target_id: str | None = None
    mode: str | None = Field(default=None, pattern="^(before|after)$")
    position: int | None = Field(default=None, ge=0)


class TaskProjectMove(BaseModel):
    project_id: str


class CompleteRequest(BaseModel):
    actual_time: int | None = Field(default=None, ge=0)


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


class RescheduleRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=500)
    # "room", "tomorrow", "next_week", or an instant of your own.
    floor: str = "tomorrow"
    preview: bool = False


class DropRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=500)


class SettingsUpdate(BaseModel):
    model_config = {"extra": "allow"}


def _parse_title_triggers(settings: dict, title: str) -> dict:
    """A new task's title, read for a deadline and/or a repeat rule.

    The local regex parser (`title_parse`) runs first — free, instant, exact
    for the fixed vocabulary it knows. Only when it finds nothing, and the
    setting allows it, does a fast-tier AI call get a second look at looser
    phrasing; a failed or disabled call is simply "nothing found", same as an
    empty regex pass. Either path returns the same shape.
    """
    parsed = title_parse.parse_title(title, datetime.now(timezone.utc), settings)
    if parsed["matched"] or not settings.get("title_parsing_ai", True):
        return parsed
    try:
        data = ai.extract_schedule(settings, title, core.now_local(settings))
    except ai.AIUnavailable as exc:
        # Best effort, like every other AI call task creation makes — a
        # missing key or a network hiccup must not stop the task being made.
        # Not queued for retry: title parsing decides the *title itself*
        # (trigger words stripped out), and by the time a retry ran the user
        # may have already edited it — there is nothing safe to replay this
        # against later.
        log.warning("title-parsing AI fallback skipped: %s", exc)
        return parsed
    if not data["has_deadline"] and not data["has_repeat"]:
        return parsed
    deadline = None
    if data["has_deadline"]:
        when = datetime.now(timezone.utc) + timedelta(minutes=data["deadline_in_minutes"])
        deadline = when.isoformat(timespec="seconds")
    repeat = None
    if data["has_repeat"]:
        repeat = {"freq": data["repeat_freq"], "interval": data["repeat_interval"],
                 "weekdays": data["repeat_weekdays"]}
    return {"clean_title": data["clean_title"], "deadline": deadline,
           "repeat": repeat, "matched": True}

def _freeze_manual_order() -> None:
    """Switch the list from sorted to hand-arranged, keeping what's on screen.

    Top-level tasks are normally shown in some computed order — urgency, or
    whichever field the sorter is set to — rather than in stored order, so the
    first drag writes the order you were looking at into `order_index` before
    moving anything. Without that, everything else would jump the moment
    manual order took effect.
    """
    settings = db.get_settings()
    if logic.sort_mode(settings)[0] == "manual":
        return
    ratios = db.completion_ratios()
    projects = db.list_projects()
    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)
    # Planned exactly the way the page plans it — one shared day book over
    # every project — so a list sorted by deadline freezes in the order it was
    # actually drawn in rather than in a slightly different one.
    all_derived = core.derive_all(projects, by_project, settings, ratios)
    # Manual order is one switch for the whole app, so every tab's top level
    # is frozen, not just the one being dragged in — otherwise the other tabs
    # would silently rearrange the next time you looked at them.
    for project in projects:
        tasks = by_project.get(project["id"], [])
        derived = all_derived[project["id"]]
        roots = [t for t in tasks if t["parent_id"] is None]
        # The order on screen, not the app's opinion of it: dragging a task
        # while the list is sorted by deadline must keep the deadline order
        # you were looking at and move only the task you dragged.
        roots.sort(key=lambda t: derived[t["id"]]["list_sort_key"])
        db.reorder_siblings(None, project["id"], [t["id"] for t in roots])
    db.update_settings({"manual_order": True, "sort_field": "manual"})


def _sorted_child_position(task: dict, parent_id: str,
                           project_id: str) -> int | None:
    """Where the sort in force puts `task` among its new parent's subtasks.

    Nesting one task inside another says where the work belongs, not what
    order the list is read in, so — unlike every other move — it leaves the
    sorter alone and asks it for the slot instead: the task lands ahead of the
    first sibling the sort puts it in front of, and at the end if it outranks
    none of them. Steps are drawn in the order they are stored, so this is one
    placement rather than a standing rule, and only the ones on screen are
    compared against: a finished step is not something a new one can be "above".

    A hand-arranged list has no sort to ask, so there the drop appends as it
    always did (None).
    """
    settings = db.get_settings()
    if logic.sort_mode(settings)[0] == "manual":
        return None
    siblings = db.sibling_ids(parent_id, project_id, exclude=task["id"])
    if not siblings:
        return None

    ratios = db.completion_ratios()
    projects = db.list_projects()
    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)
    # Read the same way the page reads it — one shared day book over every
    # project — so the slot matches the order that was on screen at the drop.
    derived = core.derive_all(projects, by_project, settings, ratios)[project_id]
    status = {t["id"]: t["status"] for t in by_project.get(project_id, [])}

    key = derived[task["id"]]["list_sort_key"]
    for i, sibling_id in enumerate(siblings):
        if status.get(sibling_id) not in logic.ACTIVE_STATUSES:
            continue
        if key < derived[sibling_id]["list_sort_key"]:
            return i
    return None


def _normalize_sort(changes: dict) -> None:
    """Keep the sorter and the manual-order flag telling the same story.

    They are two faces of one choice — "Manual" in the list's sorter *is*
    manual order — but they live in two different places on screen, so
    whichever one the page sends, the other is brought into line. A settings
    save that leaves the checkbox alone is not allowed to knock the list off
    the field it is sorted by, so the flag only speaks when it actually
    changes something.
    """
    if "sort_field" in changes:
        field = str(changes.get("sort_field") or "").strip().lower()
        changes["sort_field"] = field if field in logic.SORT_FIELDS else "smart"
        changes["manual_order"] = changes["sort_field"] == "manual"
    elif "manual_order" in changes:
        wanted = bool(changes["manual_order"])
        if wanted != (logic.sort_mode(db.get_settings())[0] == "manual"):
            changes["sort_field"] = "manual" if wanted else "smart"
    if "sort_dir" in changes:
        direction = str(changes.get("sort_dir") or "").strip().lower()
        changes["sort_dir"] = direction if direction in ("asc", "desc") else "desc"


# ---------- routes ----------

@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/api/state")
def get_state(project_id: str | None = None):
    """The page state; `project_id` reads that project's tasks without
    switching the open tab, the way the MCP server's `list_tasks` does."""
    if project_id:
        core.require_project(project_id)
    return core.state(project_id)


# ---------- projects ----------
# Tabs across the top: one list of tasks each, one open at a time. Every
# project route answers with the full page state, so switching tabs, renaming
# one, or deleting one is a single round trip like every other mutation.

@app.get("/api/projects")
def list_projects():
    state = core.state()
    return {"projects": state["projects"],
            "active_project_id": state["active_project_id"]}


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate):
    """Add a tab and switch to it — a new project is always one you want to
    start filling in immediately, so no lens stays over the top of it."""
    project = db.create_project(body.name.strip() or "New project")
    db.update_settings({"active_project": project["id"], "all_tasks_view": False,
                        "overview_view": False, "habits_view": False})
    return core.state(project["id"])


@app.patch("/api/projects/{project_id}")
def rename_project(project_id: str, body: ProjectUpdate):
    core.require_project(project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "A project needs a name")
    db.rename_project(project_id, name)
    return core.state()


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
    if core.active_project_id_raw() == project_id:
        db.update_settings({"active_project": neighbour["id"]})
    return core.state()


@app.post("/api/projects/{project_id}/move")
def move_project(project_id: str, body: ProjectMove):
    """Reorder the tab strip. Which tab is open doesn't change: dragging a
    tab rearranges the row, it doesn't switch to it."""
    core.require_project(project_id)
    others = [p["id"] for p in db.list_projects() if p["id"] != project_id]
    position = body.position
    if body.target_id:
        if not body.mode:
            raise HTTPException(400, "mode is required when target_id is given")
        if body.target_id == project_id:
            raise HTTPException(400, "A project cannot be dropped onto itself")
        core.require_project(body.target_id)
        idx = others.index(body.target_id)
        position = idx if body.mode == "before" else idx + 1
    db.move_project(project_id, position)
    return core.state()


@app.post("/api/projects/{project_id}/activate")
def activate_project(project_id: str):
    """Switch tabs. `all` and `overview` are ways of reading the lists rather
    than projects, so they are turned on here instead of remembered as the
    open one: the tab underneath stays the list that adding, braindumping and
    a new task act on.

    The Overview and Habits are the two that layer. Turning either on leaves
    All alone, so the list you came from — a project's, or every project's —
    is the one still there when you leave; turning it off is what every other
    tab in the strip does on the way to being the tab you are on. They do turn
    each other off: they are both somewhere you go, and only one of them can
    be on screen.
    """
    if project_id in (core.OVERVIEW_ID, core.HABITS_ID) and \
            not modules.enabled(project_id):
        raise HTTPException(404, modules.off_message(project_id))
    if project_id == core.OVERVIEW_ID:
        db.update_settings({"overview_view": True, "habits_view": False})
        return core.state()
    if project_id == core.HABITS_ID:
        db.update_settings({"habits_view": True, "overview_view": False})
        return core.state()
    if project_id == core.ALL_TASKS_ID:
        db.update_settings({"all_tasks_view": True, "overview_view": False,
                            "habits_view": False})
        return core.state()
    core.require_project(project_id)
    db.update_settings({"active_project": project_id, "all_tasks_view": False,
                        "overview_view": False, "habits_view": False})
    return core.state(project_id)


@app.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate):
    project_id = core.active_project_id(db.list_projects() or [db.ensure_project()])
    if body.parent_id:
        parent = core.require_task(body.parent_id)
        project_id = parent["project_id"]  # a subtask lives where its parent does
    fields = body.model_dump(exclude={"annotate"})
    for column in ("flexibility", "workday_only"):
        if fields.get(column) is None:
            # Both columns are NOT NULL with a default, and "nothing said"
            # means the default rather than a null.
            fields.pop(column, None)
    fields["project_id"] = project_id
    settings = db.get_settings()
    repeat = None
    if settings.get("title_parsing", True) and not fields.get("deadline"):
        parsed = _parse_title_triggers(settings, body.title)
        if parsed["repeat"] and not modules.enabled("recurring", settings):
            # With repeating tasks off, "every monday" is part of the title.
            parsed = {**parsed, "matched": False}
        if parsed["matched"]:
            fields["title"] = parsed["clean_title"]
            if parsed["deadline"]:
                fields["deadline"] = parsed["deadline"]
            # Only a top-level task repeats — see PUT /tasks/{id}/repeat.
            if parsed["repeat"] and not body.parent_id:
                repeat = parsed["repeat"]
    task = db.create_task(fields)
    if repeat:
        rule = logic.normalize_rule(repeat)
        if rule:
            recurring.start_series(task, rule, settings)
    core.reveal(body.parent_id)
    if body.annotate:
        estimates.annotate_tasks([task["id"]], want_scores=settings["ai_scoring"],
                                 want_start=settings["ai_start_times"])
    return core.state()


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: str, body: TaskUpdate):
    core.require_task(task_id)
    fields = body.model_dump(exclude_unset=True,
                             exclude={"clear_deadline", "clear_start_at"})
    if body.clear_deadline:
        fields["deadline"] = None
    if body.clear_start_at:
        fields["start_at"] = None
    if fields.get("status") == "in_progress":
        fields["started_at"] = db.now_iso()
    if fields.get("status") == "done":
        # One way in to "done", whichever door it came through, so a task
        # ticked from the list and one patched by hand pay the same XP.
        rest = {k: v for k, v in fields.items() if k != "status"}
        return core.state(xp_gained=core.finish(task_id, rest))
    task = db.update_task(task_id, fields) or {}
    if fields.get("status") == "discarded" and task.get("series_id"):
        # Dropping this week's copy is not dropping the job: the rhythm steps
        # past it and the next one turns up on time.
        recurring.close_occurrence(task)
    return core.state()


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    """Delete a task for good — subtasks and all, by cascade.

    Deleting one copy of a repeating job deletes that copy, not the job: the
    series takes its snapshot, steps past this occurrence and carries on. That
    is the same thing discarding it does, and it is almost always what someone
    clearing a row off today's list means. ⏹ Stop repeating is how you end the
    rhythm itself, and the delete confirmation says so.
    """
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    # The picture first, while there is still something to photograph; then
    # the row; then the rhythm steps on. That order matters: `close_occurrence`
    # makes the next copy, and it will not make one while the copy being
    # deleted is still sitting open on the list (#78).
    template = recurring.snapshot(task_id) if task.get("series_id") else None
    db.delete_task(task_id)
    if task.get("series_id"):
        recurring.close_occurrence(task, template=template)
    return core.state()


@app.post("/api/tasks/{task_id}/move")
def move_task(task_id: str, body: TaskMove):
    """Reorder or renest one task. Hand-placing one turns manual ordering on.

    A move stays inside one project — dragging is for arranging a list, not
    for crossing tabs; `POST /api/tasks/{id}/project` does that.
    """
    task = core.require_task(task_id)
    project_id = task["project_id"]

    parent_id = body.parent_id
    target = None
    if body.target_id:
        if not body.mode:
            raise HTTPException(400, "mode is required when target_id is given")
        if body.target_id == task_id:
            raise HTTPException(400, "A task cannot be dropped onto itself")
        target = core.require_task(body.target_id)
        if target["project_id"] != project_id:
            raise HTTPException(400, "That task is in a different project")
        parent_id = target["id"] if body.mode == "into" else target["parent_id"]

    if parent_id:
        if parent_id == task_id:
            raise HTTPException(400, "A task cannot be its own parent")
        parent = core.require_task(parent_id)
        if parent["project_id"] != project_id:
            raise HTTPException(400, "That task is in a different project")
        if parent_id in db.descendant_ids(task_id):
            raise HTTPException(400, "A task cannot be moved inside its own subtasks")

    # Nesting is not arranging. Dropping one task inside another says the work
    # belongs in there, which is true however the list is being read, so it
    # leaves the sorter where it is and lets the sort choose the slot; putting
    # a task between two others is a hand-placement, and that is what takes
    # the list off whatever it was sorted by.
    nesting = target is not None and body.mode == "into"
    if not nesting:
        _freeze_manual_order()

    position = body.position
    if target is not None and not nesting:
        siblings = db.sibling_ids(parent_id, project_id, exclude=task_id)
        idx = siblings.index(target["id"])
        position = idx if body.mode == "before" else idx + 1
    elif nesting:
        position = _sorted_child_position(task, parent_id, project_id)

    db.move_task(task_id, parent_id, project_id, position)
    core.reveal(parent_id)
    return core.state()


@app.post("/api/tasks/{task_id}/project")
def move_task_to_project(task_id: str, body: TaskProjectMove):
    """Send a task (with its subtasks) to another tab."""
    task = core.require_task(task_id)
    core.require_project(body.project_id)
    if task["project_id"] != body.project_id:
        db.move_task_to_project(task_id, body.project_id)
    return core.state()


@app.post("/api/tasks/{task_id}/start")
def start_task(task_id: str):
    core.require_task(task_id)
    db.update_task(task_id, {"status": "in_progress", "started_at": db.now_iso()})
    return core.state()


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, body: CompleteRequest):
    task = core.require_task(task_id)
    actual = body.actual_time
    if actual is None and task["started_at"]:
        started = logic.parse_dt(task["started_at"])
        if started:
            actual = max(1, round((datetime.now(timezone.utc) - started).total_seconds() / 60))
    fields: dict = {}
    if actual is not None:
        fields["actual_time"] = actual
    return core.state(xp_gained=core.finish(task_id, fields))

@app.get("/api/next")
def get_next():
    """The one task to do next, picked from whatever list is on screen — the
    open tab's, or every project's while the All tab is the one open, so the
    task wearing the "next up" badge is the task this hands back."""
    projects = db.list_projects() or [db.ensure_project()]
    settings = db.get_settings()
    all_view = core.all_tasks_view(settings, projects)
    project_id = core.active_project_id(projects)
    tasks = db.list_tasks(None if all_view else project_id)
    derived = logic.compute(tasks, core.list_settings(settings, all_view),
                            db.completion_ratios())
    nxt = logic.next_task(tasks, derived)
    if not nxt:
        return {"task": None}
    return {"task": {**nxt, **derived[nxt["id"]]}}

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
        core.require_task(item.task_id)
        wanted[item.task_id] = when

    # Group by project so each task's current (possibly auto-assigned) deadline
    # — the thing the shift is measured from — is computed the same way the
    # list computes it.
    by_project: dict[str, list[dict]] = {}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)

    moves: dict[str, dict[str, str]] = {}
    for task_id, when in wanted.items():
        task = db.get_task(task_id)
        tasks = by_project.get(task["project_id"], [])
        derived = logic.compute(tasks, settings, ratios)
        plan = logic.nudge_plan(tasks, derived, task_id, when)
        for tid, fields in plan.items():
            moves.setdefault(tid, dict(fields))
        # The task this entry actually names wins outright, at both ends: a
        # parent's slide must not be what decides where its explicitly nudged
        # child starts, only where the rest of the plan around it does.
        moves[task_id] = dict(plan.get(task_id, {}))
        moves[task_id]["deadline"] = when.isoformat(timespec="seconds")

    for tid, fields in moves.items():
        db.update_task(tid, fields)
    return core.state()


@app.post("/api/reschedule")
def reschedule(body: RescheduleRequest):
    """Clear a pile of overdue work onto the days that can actually hold it.

    The difference from `/api/nudge` is the whole point of it. Nudge writes the
    instant you picked, which is right for one task and wrong for twelve: the
    same instant on every one of them is not a plan, it is the same pile on a
    different day. This asks the day book where each of them fits, in the order
    they deserve, and hands back the spread.

    `preview` answers the question without writing anything, so the dialog can
    show you what it is about to do and you can say no.
    """
    tasks = db.list_tasks()
    known = {t["id"] for t in tasks}
    for task_id in body.task_ids:
        if task_id not in known:
            raise HTTPException(404, f"Task not found: {task_id}")

    settings = db.get_settings()
    ratios = db.completion_ratios()
    by_project: dict[str, list[dict]] = {}
    for t in tasks:
        by_project.setdefault(t["project_id"], []).append(t)

    # Planned per project, because that is the unit `compute` works in, and
    # the plans are then read back together: the days themselves are shared,
    # which is what `logic.reschedule_plan` books against.
    moves: list[dict] = []
    for project_id, group in by_project.items():
        ids = [t["id"] for t in group if t["id"] in set(body.task_ids)]
        if ids:
            moves.extend(logic.reschedule_plan(group, settings, ids,
                                               body.floor, ratios))
    moves.sort(key=lambda m: m["deadline"])
    if body.preview:
        return {"moves": moves, "applied": False}

    for move in moves:
        db.update_task(move["task_id"], {"deadline": move["deadline"]})
    return {"moves": moves, "applied": True, "state": core.state()}


@app.post("/api/tasks/drop")
def drop_tasks(body: DropRequest):
    """"These are not happening." A pile is mostly work that stopped being
    real days ago, and saying so is more honest than giving all of it a date
    nobody believes. Dropping a repeating copy drops that copy, not the job,
    exactly as it does one at a time."""
    dropped: list[dict] = []
    for task_id in body.task_ids:
        core.require_task(task_id)
    for task_id in body.task_ids:
        was = db.get_task(task_id)
        task = db.update_task(task_id, {"status": "discarded"})
        if task and task.get("series_id") and was["status"] in logic.ACTIVE_STATUSES:
            recurring.close_occurrence(task)
        dropped.append({"task_id": task_id, "was": was["status"]})
    return {"dropped": dropped, "state": core.state()}


@app.post("/api/replan")
def replan_now():
    """Remake the plan, now, because you asked.

    Everything else that remakes it is a consequence of something you did (see
    `scheduler.py`); this is the button for when you simply want it thought
    through again. It bumps the rev, and the state it returns is planned
    against it.
    """
    db.bump_plan_rev()
    return core.state()


@app.get("/api/settings")
def get_settings():
    settings = db.get_settings()
    # Never echo the key back to the page; report only whether one exists.
    has_key = bool((settings.pop("api_key", "") or "").strip()
                   or os.environ.get("ANTHROPIC_API_KEY"))
    settings["has_api_key"] = has_key
    has_clickup_token = bool((settings.pop("clickup_api_token", "") or "").strip()
                             or os.environ.get("CLICKUP_API_TOKEN"))
    settings["has_clickup_token"] = has_clickup_token
    # Derived, not stored: what the day cap has actually learned to be, so the
    # dialog can show it next to the number you set.
    settings["capacity"] = core.capacity()
    # Likewise derived: what today has cost so far, and where that leaves the
    # routing, next to the budget that decides it.
    settings["spend"] = core.spend(settings)
    # Static: what each module is called and does, for the Modules section.
    # Whether it is on is the stored `modules` setting beside it.
    settings["module_catalog"] = modules.catalog()
    return settings


@app.put("/api/settings")
def put_settings(body: SettingsUpdate):
    changes = body.model_dump()
    changes.pop("has_api_key", None)
    changes.pop("has_clickup_token", None)
    changes.pop("capacity", None)  # derived; the page only ever echoes it back
    changes.pop("spend", None)     # likewise
    changes.pop("module_catalog", None)
    if changes.get("api_key") == "":
        changes.pop("api_key")  # empty field means "leave unchanged"
    if changes.get("clickup_api_token") == "":
        changes.pop("clickup_api_token")  # empty field means "leave unchanged"
    _normalize_sort(changes)
    db.update_settings(changes)
    return get_settings()


# Each module's routes, every one of them answering 404 while its module is
# off. See `modules.py`.
for module_routes in (clickup_routes, magic_todo, braindump, estimates, calendar,
                      overview, habits, help, recurring_routes, focus, alarms):
    app.include_router(module_routes.router)


# AI agents: the MCP server at /mcp. Added before the static mount, which
# would otherwise answer for every path. See `mcp_server.py`.
mcp = mcp_server.build()
app.router.routes.extend(
    Route(r.path, modules.Gate("mcp", r.app))
    for r in mcp.streamable_http_app(stateless_http=True, json_response=True).routes)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
