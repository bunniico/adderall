"""The calendar: every dated task across every project."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from .. import core, db, logic, modules, recurring

router = APIRouter(dependencies=[Depends(modules.require("calendar"))])


# How much of the rhythms' future the calendar draws — three months, which is
# further than anyone pages. The day book looks further ahead still
# (`recurring.FORECAST_DAYS`, which has to cover the planner's search window);
# there is no sense in shipping all of that to a page nobody scrolls that far.
CALENDAR_FORECAST_DAYS = 90


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

    forecast = recurring.forecast(settings=settings, ratios=ratios)
    book: dict = {}
    all_derived = core.derive_all(projects, by_project, settings, ratios, forecast,
                                  book=book)
    events: list[dict] = []
    for project in projects:
        tasks = by_project.get(project["id"], [])
        derived = all_derived[project["id"]]
        series = recurring.by_task(tasks, settings)
        children_count: dict[str, int] = {}
        for t in tasks:
            if t["parent_id"] and t["status"] != "discarded":
                children_count[t["parent_id"]] = children_count.get(t["parent_id"], 0) + 1
        for t in tasks:
            d = derived[t["id"]]
            # Steps are drawn inside their root's block, not beside it: a tree
            # is one commitment, and six chips for six steps is the same
            # afternoon counted six times. They carry no deadline of their own
            # now (see `logic.compute`), so this only says out loud what the
            # next line would do anyway.
            if t["parent_id"]:
                continue
            # Discarded work is off the plan entirely; done work stays, because
            # a calendar you can look back at is half of what a calendar is for.
            if t["status"] == "discarded" or not d.get("deadline"):
                continue
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
                # When it wanted to begin, which for anything the app placed
                # itself is also where its block starts.
                "start_at": t["start_at"],
                "estimated_time": t["estimated_time"],
                "buffered_estimate": d["buffered_estimate"],
                "buffer_applied": d["buffer_applied"],
                # How long the block is, and how much of that is the time tax
                # rather than the work — the day view draws the difference.
                "length_min": d["length_min"],
                "raw_length_min": d["raw_length_min"],
                "impact": t["impact"],
                "effort": t["effort"],
                "quadrant": d["quadrant"],
                "urgency": d["urgency"],
                "score": d["score"],
                "has_subtasks": d["has_subtasks"],
                "subtask_count": children_count.get(t["id"], 0),
                # A block you will see again next week reads differently from
                # a one-off with the same date on it, so the calendar says so.
                "recurrence": series.get(t["id"]),
                "path": logic.ancestor_titles(tasks, t),
                # Where the work actually sits, which is not always one run
                # back from the deadline: work due first thing was done the
                # evening before, and work too big for a day is in pieces.
                "blocks": d["blocks"],
                # And the minutes that did not fit anywhere before the
                # deadline, so the calendar can say the blocks above are the
                # part of the job that has a home rather than the whole of it.
                "overflow_min": d["overflow_min"],
                # A real task, as against one of the outlines below.
                "projected": False,
            })
    events.extend(_forecast_events(forecast, projects, by_project, settings,
                                   ratios, book.get("planner")))
    return events


def _forecast_events(forecast: list[dict], projects: list[dict],
                     by_project: dict[str, list[dict]], settings: dict,
                     ratios: list[float],
                     planner: logic.DayPlanner | None = None) -> list[dict]:
    """The rhythms' future, as blocks that do not exist yet.

    A repeating job puts one copy on your list at a time, which is the right
    answer for a list and leaves the calendar telling you a lie: a fortnight
    of empty afternoons for a fortnight in which you will work every one of
    them. These are the occurrences still to come — when they land, how long
    they will take, whose they are — carrying everything a block needs to be
    drawn and counted, and marked `projected` so the page can draw them as
    the forecast they are. There is nothing to tick off and nothing to nudge:
    a click opens the copy that *is* on your list, which is the thing you can
    actually change.
    """
    now = datetime.now(timezone.utc)
    buf = logic.effective_buffer(settings, ratios)
    threshold = int(settings.get("matrix_threshold", 5))
    names = {p["id"]: p["name"] for p in projects}
    horizon = now + timedelta(days=CALENDAR_FORECAST_DAYS)

    open_of: dict[str, str] = {}
    for tasks in by_project.values():
        for t in tasks:
            if t.get("series_id") and t["status"] in logic.ACTIVE_STATUSES:
                open_of.setdefault(t["series_id"], t["id"])

    described: dict[str, dict | None] = {}
    events: list[dict] = []
    for occ in forecast:
        if occ["at"] > horizon:
            continue
        sid = occ["series_id"]
        if sid not in described:
            described[sid] = recurring.describe(db.get_series(sid), settings)
        template, length = occ["template"], occ["minutes"]
        # The beat is when the work happens; the deadline is the end of the
        # window it opens. They are the same instant unless the rule named an
        # hour, in which case that hour starts the work rather than ending it
        # (`logic.occurrence_window`).
        due = occ.get("due_at") or occ["at"]
        opens = occ.get("start_at")
        pressure = logic.urgency(due, length, now)
        events.append({
            "id": occ["key"],
            "title": template.get("title") or "Recurring task",
            # Deliberately not the template's description: there is nothing
            # here to open and read, and one long note copied onto ninety
            # outlines is ninety copies of it on the wire.
            "description": "",
            "parent_id": None,
            "project_id": occ["project_id"],
            "project_name": names.get(occ["project_id"], ""),
            # Not a status a task can have: nothing here is a task yet.
            "status": "planned",
            "deadline": due.isoformat(timespec="seconds"),
            "deadline_source": "recurring",
            "start_at": opens.isoformat(timespec="seconds") if opens else None,
            "estimated_time": template.get("estimated_time"),
            "buffered_estimate": length,
            "buffer_applied": buf,
            "length_min": length,
            "raw_length_min": max(1, round(length / (1.0 + buf))),
            "impact": template.get("impact"),
            "effort": template.get("effort"),
            "quadrant": logic.quadrant(template.get("impact"),
                                       template.get("effort"), threshold),
            "urgency": pressure,
            "score": logic.priority_score(template.get("impact"),
                                          template.get("effort"), pressure, length),
            # One outline stands for the whole tree it will be planted as, so
            # the day it lands on is charged for it exactly once.
            "has_subtasks": False,
            "subtask_count": len(template.get("subtasks") or []),
            "recurrence": described[sid],
            "path": [],
            "blocks": (logic.task_blocks(planner, occ["key"],
                                         {"deadline": due.isoformat(),
                                          "length_min": length})
                       if planner is not None else []),
            "projected": True,
            "occurrence": occ["number"],
            "source_task_id": open_of.get(sid),
        })
    return events


@router.get("/api/calendar")
def get_calendar():
    """Everything with a date on it, from every project, scored.

    One payload for all three views: the page slices it into days, weeks and
    months locally, so ‹ › navigation and changing a filter never wait on the
    network.

    `capacity` rides along: the day cap the views warn against overshooting,
    plus the history that moved it, so the warning can explain itself instead
    of quoting a number from nowhere.
    """
    return {"events": _calendar_events(), "capacity": core.capacity()}
