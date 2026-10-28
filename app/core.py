"""What every part of the app shares: the page state, the plan it is drawn
from, and the small helpers the routes in `main.py` and `routes/` all lean on.

The core routes live in `main.py` and each module's routes in `routes/`; both
import from here, and nothing here imports either of them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from . import db, logic, modules, recurring


# ---------- helpers ----------

def _tree(tasks: list[dict], derived: dict[str, dict],
          series: dict[str, dict] | None = None) -> list[dict]:
    """Nest a flat task list into roots + subtasks, derived fields merged in.

    `series` is {task_id: the rhythm it belongs to}, already put into words by
    `recurring.describe`, so the badge on the task says the same thing the
    repeat dialog does without the page having to work it out twice.
    """
    series = series or {}
    by_id: dict[str, dict] = {}
    for t in tasks:
        by_id[t["id"]] = {**t, **derived[t["id"]],
                          "recurrence": series.get(t["id"]), "subtasks": []}
    roots = []
    for t in tasks:
        node = by_id[t["id"]]
        if t["parent_id"] and t["parent_id"] in by_id:
            by_id[t["parent_id"]]["subtasks"].append(node)
        else:
            roots.append(node)
    return roots


# The All tab: every project's tasks compiled into one list. It is a lens over
# the tabs rather than a place a task can live, so it has no row in the
# database — the page asks for it by this name, and the tab you were actually
# working in stays remembered underneath it.
ALL_TASKS_ID = "all"


# The Overview tab, likewise asked for by name. Not a list either: it is what
# the lists add up to.
OVERVIEW_ID = "overview"


def overview_view(settings: dict) -> bool:
    """Is the Overview tab the one on screen?

    Never while its module is off. Otherwise unconditional, where the All tab
    needs a second project to be worth anything: "how much is left, and what
    next" is a fair question to ask of one list, and the answer is drawn from
    the same numbers either way.
    """
    return bool(settings.get("overview_view")) and modules.enabled("overview", settings)


# The Habits tab. Asked for by name like the two above, and like them it is
# not a project — routines live in no list, so there is nothing underneath it
# to switch away from.
HABITS_ID = "habits"


def habits_view(settings: dict) -> bool:
    """Is the Habits tab the one on screen?"""
    return bool(settings.get("habits_view")) and modules.enabled("habits", settings)


def all_tasks_view(settings: dict, projects: list[dict]) -> bool:
    """Is the All tab the one on screen?

    Never with a single project: "all of them" is that list, and a tab showing
    exactly what the tab beside it shows is noise. Which also means the lens
    puts itself away when the second-to-last project is deleted, without
    anything having to remember to turn it off.
    """
    return bool(settings.get("all_tasks_view")) and len(projects) > 1


def list_settings(settings: dict, all_view: bool) -> dict:
    """The sorter as the list on screen reads it.

    Hand-arranged order belongs to one list: where you dragged a task in one
    tab says nothing about where it sits among another tab's. So the All tab
    is read by urgency however the open tab was last arranged, and the page
    hides the Manual option for as long as it is on.
    """
    if not all_view:
        return settings
    return {**settings, "sort_field": "smart", "manual_order": False}


def active_project_id_raw() -> str:
    """Whatever id is stored, without resolving it against existing projects."""
    return (db.get_settings().get("active_project") or "").strip()


def active_project_id(projects: list[dict]) -> str:
    """The project tab the page is on, remembered across reloads.

    Falls back to the first tab whenever the stored one is gone (deleted on
    another device, or a fresh database), and writes the fallback back so the
    answer stays stable.
    """
    stored = active_project_id_raw()
    if any(p["id"] == stored for p in projects):
        return stored
    db.update_settings({"active_project": projects[0]["id"]})
    return projects[0]["id"]


def capacity(settings: dict | None = None) -> dict:
    """How much work one day is allowed to hold, and how that was arrived at.

    The 8-hour cap is a setting; this is what it has learned to be. The number
    and its workings travel together so the calendar can say which one it is
    warning against and why — see `logic.capacity_plan`.
    """
    return logic.capacity_plan(settings or db.get_settings(), db.completed_history())


def spend(settings: dict | None = None) -> dict:
    """What the AI has cost today, against the budget, and what that is doing.

    Every field here is derived — today's rows summed, the ladder in
    `logic` read — so the page never has to work out where a number sits
    between three thresholds, and the badge it draws says the same thing the
    log line at the call site says.
    """
    settings = settings or db.get_settings()
    budget = logic.daily_budget(settings)
    today = db.spend_since(logic.spend_window_start(settings))
    stage = logic.throttle_stage(today, budget)
    return {
        "today": round(today, 4),
        "budget": budget,
        "stage": stage,
        "note": logic.THROTTLE_NOTES[stage],
        "tiers": {tier: logic.throttled_tier(tier, stage)
                  for tier in ("fast", "balanced", "deep")},
    }


def xp_state(settings: dict, ratios: list[float], gained: int | None = None) -> dict:
    """Where the level stands and the two averages under it.

    One shape for both readers — the rail's `/api/state` and the Overview —
    so a number that appears in both places cannot come to mean two different
    things in them. `gained` is the only difference: it is the cue to animate
    the bar, so it rides on the reply to the call that earned it and nowhere
    else.
    """
    payload = {
        **logic.level_progress(db.get_xp()),
        "hourly": logic.average_hourly_xp(
            db.xp_estimate_pairs(), logic.effective_buffer(settings, ratios)),
        "daily": logic.average_daily_xp(db.xp_since(logic.XP_WINDOW_DAYS)),
        "daily_days": logic.XP_WINDOW_DAYS,
    }
    return payload if gained is None else {**payload, "gained": gained}


def derive_all(projects: list[dict], by_project: dict[str, list[dict]],
               settings: dict, ratios: list[float],
               forecast: list[dict] | None = None,
               book: dict | None = None) -> dict[str, dict]:
    """Derived fields for every project, planned against one shared day book.

    Two passes, deliberately. Everything already pinned to a time is booked
    first, across every project, and only then does the app place the work it
    schedules itself. A task in one tab must never be dropped on top of a
    commitment in another: the calendar spans every project, and so does the
    day it is spending.

    Work that comes back is booked in that first pass too. Only one copy of a
    repeating job is ever on the list, so a week of eight-hour weekdays looks
    like an empty week to a planner that can only see tasks — and it would
    hand you Tuesday afternoon for something new every time. The rhythms'
    future is a commitment you have already made; it goes in the book with the
    rest of them (see `recurring.forecast`).
    """
    now = datetime.now(timezone.utc)
    # Deciding or reading? The plan on the tasks is current while nothing has
    # changed since it was made, and a read then simply reads it — which is
    # what makes two reads agree. See `scheduler.py` for the whole rule.
    state = db.plan_state()
    replan = state["rev"] != state["planned_rev"]

    planner = logic.day_planner(settings, capacity(settings)["minutes"], now=now)
    # Which days each repeating task's own rule names. Looked up once, across
    # every project, because the planner cannot ask: a copy on the list looks
    # like any other task to it (`recurring.repeat_days`).
    repeat_days = recurring.repeat_days(
        [t for tasks in by_project.values() for t in tasks])
    for project in projects:
        logic.reserve_fixed(planner, by_project.get(project["id"], []),
                            settings, ratios, repeat_days=repeat_days)
    if forecast is None:
        forecast = recurring.forecast(settings=settings, ratios=ratios)
    recurring.reserve_forecast(planner, forecast)
    # The caller can ask for the book itself: the forecast's blocks live in it
    # and nowhere else, since an occurrence is not a task to derive anything
    # from. `book["planner"] = ...` rather than a second return value, so the
    # three callers that do not care are left alone.
    if book is not None:
        book["planner"] = planner
    derived = {p["id"]: logic.compute(by_project.get(p["id"], []), settings,
                                      ratios, now=now, planner=planner,
                                      replan=replan, repeat_days=repeat_days)
               for p in projects}
    if replan:
        _write_plan(projects, by_project, derived, state["rev"], now, settings)
    return derived


def _write_plan(projects: list[dict], by_project: dict[str, list[dict]],
                derived: dict[str, dict], rev: int, now: datetime,
                settings: dict) -> None:
    """Write down where the app just decided each thing goes.

    Only the placements it chose itself: a deadline you set is yours and is
    not the plan's to remember. Stored on the task so the next read can read
    it rather than decide it again, and stamped with the rev it covers so the
    read after a change knows to think again.
    """
    for project in projects:
        for task in by_project.get(project["id"], []):
            d = derived[project["id"]].get(task["id"])
            if d is None:
                continue
            # A task that is over has no plan. The planner stopped booking it
            # the moment it closed, so anything stored here is a slot it no
            # longer holds, and its blocks are now a record of when the work
            # happened (`logic.finished_blocks`) rather than a claim on time.
            live = task["status"] in logic.ACTIVE_STATUSES
            auto = live and d.get("deadline_source") == "auto" and d.get("deadline")
            planned_at = d["deadline"] if auto else None
            blocks = d.get("blocks") if auto else None
            if task.get("planned_at") != planned_at or \
               task.get("planned_blocks") != blocks:
                db.save_plan(task["id"], planned_at, blocks)
    local = now.astimezone(logic.resolve_tz(settings.get("timezone")))
    db.record_plan(rev, now.isoformat(timespec="seconds"), local.date().isoformat())


def state(project_id: str | None = None, xp_gained: int = 0) -> dict:
    """Everything the page renders: the task tree the open tab is showing, the
    tab strip, the cross-project deadline list the alarms run off, and where
    the XP total stands.

    Only the active project's tasks are sent as a tree — a tab you are not
    looking at is not on screen — but deadlines are gathered from every
    project, because a transition alarm you miss because its task lives in
    another tab is exactly the failure this app exists to prevent.

    Unless the All tab is the one open, in which case the tree *is* every
    project's tasks, each root carrying the name of the list it came from. It
    is still one payload and still one round trip: which tab is on screen
    decides what the tree holds, not how many calls it takes to get it.
    """
    settings = db.get_settings()
    ratios = db.completion_ratios()
    projects = db.list_projects() or [db.ensure_project()]
    active_id = project_id or active_project_id(projects)
    all_view = all_tasks_view(settings, projects)

    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)

    counts = db.open_task_counts()
    all_derived = derive_all(projects, by_project,
                              list_settings(settings, all_view), ratios)
    alarm_tasks: list[dict] = []
    tree: list[dict] = []
    next_task_id = None
    compiled: list[dict] = []
    compiled_derived: dict[str, dict] = {}
    for project in projects:
        tasks = by_project.get(project["id"], [])
        derived = all_derived[project["id"]]
        for t in tasks:
            d = derived[t["id"]]
            if t["status"] in logic.ACTIVE_STATUSES and d.get("deadline"):
                alarm_tasks.append({
                    "id": t["id"], "title": t["title"], "deadline": d["deadline"],
                    "project_id": project["id"], "project_name": project["name"],
                    # Its planned slot, for the start cues (see events.py).
                    "status": t["status"], "blocks": d.get("blocks") or [],
                })
        if all_view:
            # Which list a task came from is the one thing a compiled list
            # loses, so every task carries it home.
            compiled.extend({**t, "project_name": project["name"]} for t in tasks)
            compiled_derived.update(derived)
        elif project["id"] == active_id:
            tree = _tree(tasks, derived, recurring.by_task(tasks, settings))
            nxt = logic.next_task(tasks, derived)
            next_task_id = nxt["id"] if nxt else None
    if all_view:
        tree = _tree(compiled, compiled_derived,
                     recurring.by_task(compiled, settings))
        nxt = logic.next_task(compiled, compiled_derived)
        next_task_id = nxt["id"] if nxt else None

    return {
        "tasks": tree,
        "next_task_id": next_task_id,
        "projects": [{**p, "open_tasks": counts.get(p["id"], 0)} for p in projects],
        "active_project_id": active_id,
        # Which tab is lit. The active project stays whatever it was, because
        # the All tab is a way of reading the lists, not a list to add to.
        "all_tasks": all_view,
        # And whether the Overview is over the top of it. It sits above both
        # of the above rather than replacing either, so the tab underneath is
        # still there to go back to — see `activate_project`.
        "overview": overview_view(settings),
        # ...and the same for Habits, which layers the same way and for the
        # same reason: your routines are not one of the lists, so opening them
        # should not decide which list you come back to.
        "habits": habits_view(settings),
        "alarm_tasks": alarm_tasks,
        # Levels ride along with every state read so a reload never shows a
        # stale bar. `gained` is only ever non-zero on the reply to the call
        # that earned it, which is the page's cue to animate rather than
        # silently jump.
        "xp": xp_state(settings, ratios, gained=xp_gained),
        # Rides along for the same reason the XP bar does: the badge that says
        # the app has gone cheap for the day has to appear the moment it is
        # true, not the next time someone opens the settings dialog.
        "spend": spend(settings),
        # When the app last worked out where things go. On screen, because
        # "I'm not sure when it runs" is a fair thing to say about a scheduler
        # that never tells you. Read *after* the derive above, so it is the
        # plan this payload was built from.
        "plan": db.plan_state(),
    }


def now_local(settings: dict) -> str:
    """The clock the AI is told it is reading, in the user's own timezone.

    A start time is the one thing the model is asked for that it cannot work
    out from the task alone: "eat dinner" is six hours off at noon and thirty
    minutes off at half past five, and only the local hour says which.
    """
    tz = logic.resolve_tz(settings.get("timezone"))
    return datetime.now(tz).strftime("%A %d %B %Y, %H:%M (%Z)").strip()


def reveal(task_id: str | None) -> None:
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


def require_task(task_id: str) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


def require_project(project_id: str) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def project_derived(project_id: str) -> dict[str, dict]:
    """Derived fields for one project's tasks — the same numbers the page is
    already showing, planned against every project's shared day book."""
    settings = db.get_settings()
    ratios = db.completion_ratios()
    projects = db.list_projects() or [db.ensure_project()]
    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)
    return derive_all(projects, by_project, settings, ratios).get(project_id, {})


def finish(task_id: str, fields: dict | None = None) -> int:
    """Mark a task and everything still open under it done, and pay the XP out.

    The scores are read *before* anything is marked done, because finishing a
    task drops its urgency — and so its score — to nothing: what it pays has
    to be what it was worth while you still had it to do.

    Only leaves pay. A container's score is borrowed from the steps beneath it
    (see `logic._rollup_score`), so paying for both would pay twice for one
    afternoon's work; completing a parent finishes those steps, and they are
    what pays. A task that has already been paid for never pays again, however
    often it is reopened and finished — the XP you earned stays earned, and
    nothing is farmable by ticking the same box twice.
    """
    task = db.get_task(task_id)
    if task is None:
        return 0
    # Whether this was still open decides whether finishing it means anything
    # to its rhythm. Ticking a copy that was already closed — one you missed
    # and did anyway, one ticked twice — must not step the series a second
    # time and skip a beat nobody has done.
    was_open = task["status"] in logic.ACTIVE_STATUSES
    derived = project_derived(task["project_id"])
    awards: list[tuple[str, int]] = []
    for tid in [task_id, *db.descendant_ids(task_id)]:
        row = db.get_task(tid)
        if row is None or row["status"] not in logic.ACTIVE_STATUSES:
            continue                      # already finished, or discarded
        if row["xp_awarded"] is not None:
            continue                      # paid for once already
        d = derived.get(tid)
        if d is None or d["has_subtasks"]:
            continue                      # a container pays through its steps
        awards.append((tid, logic.task_xp(d["score"])))

    db.update_task(task_id, {**(fields or {}), "status": "done"})
    for tid in db.descendant_ids(task_id):
        child = db.get_task(tid)
        if child and child["status"] in ("todo", "in_progress"):
            db.update_task(tid, {"status": "done"})

    gained = 0
    for tid, amount in awards:
        db.award_xp(tid, amount)
        gained += amount
    # Last, once everything is marked done: finishing one occurrence of a
    # repeating job is what opens the next. Doing it here means the list, a
    # checkbox, the detail modal and Focus mode all repeat the same way,
    # because all four already come through here.
    if task.get("series_id") and was_open:
        recurring.close_occurrence(db.get_task(task_id) or task)
    return gained


def queued_state(kind: str, message: str) -> dict:
    """The normal state payload, plus a note that this action didn't run yet
    because the server itself has no route to the internet — it's queued and
    will apply on its own once queue.py gets it through. See queue.py."""
    payload = state()
    payload["queued"] = {"kind": kind, "message": message}
    return payload