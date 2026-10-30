"""The Overview tab: what every list adds up to."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from .. import core, db, logic, modules, recurring

router = APIRouter(dependencies=[Depends(modules.require("overview"))])


# ---------- overview ----------
# What every list adds up to. The tabs answer "what is on this list"; this
# answers "how much is there, what next, and am I getting anywhere" — which
# are questions about all of them at once, and questions no single list can be
# asked.

OVERVIEW_TOP = 6           # "do this next" rows — a shortlist, not a list
OVERVIEW_UPCOMING = 6      # ...and the same again for what is coming at you
OVERVIEW_TREND_DAYS = 30   # how far the finished-work chart looks back
OVERVIEW_SOON_DAYS = 7     # what "due soon" means on the tiles


def _overview_row(task: dict, d: dict, project: dict, tasks: list[dict],
                  series: dict) -> dict:
    """One task as the Overview lists it.

    Carries what the rows draw *and* what the detail dialog needs, because
    clicking one opens it in place: the task is usually in another tab
    entirely, so this payload is the only copy of it the page has — the same
    arrangement the calendar already runs on.
    """
    return {
        "id": task["id"],
        "title": task["title"],
        "description": task["description"],
        "parent_id": task["parent_id"],
        "project_id": project["id"],
        "project_name": project["name"],
        "status": task["status"],
        "deadline": d.get("deadline") or d.get("inherited_deadline"),
        # Only a real deadline of its own has a source; a step showing the
        # date its tree is aimed at must not read as one somebody set.
        "deadline_source": d["deadline_source"] if d.get("deadline") else None,
        "start_at": task["start_at"],
        "estimated_time": task["estimated_time"],
        "buffered_estimate": d["buffered_estimate"],
        "impact": task["impact"],
        "effort": task["effort"],
        "flexibility": task["flexibility"],
        "workday_only": task["workday_only"],
        "repeat_carry": task["repeat_carry"],
        "quadrant": d["quadrant"],
        "urgency": d["urgency"],
        "score": d["score"],
        "length_min": d["length_min"],
        "has_subtasks": d["has_subtasks"],
        "rollup_remaining": d["rollup_remaining"],
        "blocks": d["blocks"],
        "recurrence": series.get(task["id"]),
        # Which tree a step came from. A step's title is half a sentence on
        # its own — "ask her sister" says nothing without "buy gift" above it.
        "path": logic.ancestor_titles(tasks, task),
    }


def _minutes_left(d: dict) -> int:
    """What is actually left on a top-level task, in buffered minutes.

    A container is worth the sum of what it still holds, not the estimate
    somebody put on the container — the same rule the rail counts by, so the
    two numbers never disagree.
    """
    if d["has_subtasks"]:
        return d["rollup_remaining"] or 0
    return d["buffered_estimate"] or 0


def _booked_today(blocks: list, start: datetime, end: datetime) -> int:
    """Minutes of a task's booked blocks that fall inside today.

    Blocks are UTC instants and today is a local day, so this is an overlap
    rather than a match: work that runs past midnight counts on both days, for
    exactly the minutes it spends in each.
    """
    total = 0
    for block in blocks or []:
        begin = logic.parse_dt(block[0])
        finish = logic.parse_dt(block[1])
        if begin is None or finish is None:
            continue
        overlap = min(finish, end) - max(begin, start)
        total += max(0, int(overlap.total_seconds() // 60))
    return total


def _overview_trend(days: int, tz) -> list[dict]:
    """Work finished per local day, every day in the window — zeroes included.

    A chart drawn only from the days you finished something on quietly
    rewrites the week: four bars in a row read as four days on the trot, and
    the three empty days between them vanish. The gaps are the point.
    """
    counts: dict[str, int] = {}
    minutes: dict[str, int] = {}
    for row in db.finished_log(days):
        when = logic.parse_dt(row.get("finished_at"))
        if when is None:
            continue
        key = when.astimezone(tz).date().isoformat()
        counts[key] = counts.get(key, 0) + 1
        minutes[key] = minutes.get(key, 0) + int(row.get("minutes") or 0)
    today = datetime.now(tz).date()
    series = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        series.append({"day": day, "finished": counts.get(day, 0),
                       "minutes": minutes.get(day, 0)})
    return series


def _overview() -> dict:
    """Every list, counted.

    One payload, the same shape however many projects there are, and derived
    through the same shared day book the calendar and the task list are
    derived through — so "4h 30m booked today" here is the same four and a
    half hours the calendar draws, rather than a second opinion about them.
    """
    settings = db.get_settings()
    ratios = db.completion_ratios()
    projects = db.list_projects() or [db.ensure_project()]
    # Every list at once, so hand-arranged order means nothing here for the
    # same reason it means nothing on the All tab: where you dragged a task in
    # one tab says nothing about where it sits among another tab's.
    lens = core.list_settings(settings, True)

    by_project: dict[str, list[dict]] = {p["id"]: [] for p in projects}
    for t in db.list_tasks():
        by_project.setdefault(t["project_id"], []).append(t)
    all_derived = core.derive_all(projects, by_project, lens, ratios)

    now = datetime.now(timezone.utc)
    tz = logic.resolve_tz(settings.get("timezone"))
    today = datetime.now(tz).date()
    day_start = datetime(today.year, today.month, today.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    soon = now + timedelta(days=OVERVIEW_SOON_DAYS)

    open_tasks = overdue = due_soon = 0
    minutes_left = booked_today = 0
    per_project: list[dict] = []
    per_quadrant: dict[str, dict] = {
        key: {"quadrant": key, "tasks": 0, "minutes": 0}
        for key in ("quick_win", "major_project", "fill_in", "thankless", "none")
    }
    candidates: list[tuple] = []   # (sort_key, row) — the shortlist to rank
    dated: list[dict] = []

    for project in projects:
        tasks = by_project.get(project["id"], [])
        derived = all_derived[project["id"]]
        series = recurring.by_task(tasks, settings)
        project_minutes = project_open = 0
        for t in tasks:
            d = derived[t["id"]]
            if t["status"] not in logic.ACTIVE_STATUSES:
                continue
            open_tasks += 1
            project_open += 1
            if d["actionable"]:
                candidates.append(
                    (d["sort_key"], _overview_row(t, d, project, tasks, series)))
            # Everything below is about scheduled work, and only a top-level
            # task is scheduled: a tree is one commitment, and counting its
            # steps as well would count the same afternoon twice.
            if t["parent_id"]:
                continue
            left = _minutes_left(d)
            minutes_left += left
            project_minutes += left
            bucket = per_quadrant[d["quadrant"] or "none"]
            bucket["tasks"] += 1
            bucket["minutes"] += left
            booked_today += _booked_today(d["blocks"], day_start, day_end)
            when = logic.parse_dt(d.get("deadline"))
            if when is None:
                continue
            if when < now:
                overdue += 1
            elif when <= soon:
                due_soon += 1
            dated.append(_overview_row(t, d, project, tasks, series))
        per_project.append({"id": project["id"], "name": project["name"],
                            "open": project_open, "minutes": project_minutes})

    candidates.sort(key=lambda pair: pair[0])
    trend = _overview_trend(OVERVIEW_TREND_DAYS, tz)
    dated.sort(key=lambda row: row["deadline"])
    return {
        "stats": {
            "open": open_tasks,
            "overdue": overdue,
            "due_soon": due_soon,
            "soon_days": OVERVIEW_SOON_DAYS,
            "minutes_left": minutes_left,
            "booked_today": booked_today,
            "finished": sum(day["finished"] for day in trend),
            "finished_minutes": sum(day["minutes"] for day in trend),
            "trend_days": OVERVIEW_TREND_DAYS,
            "projects": len(projects),
        },
        "top": [row for _, row in candidates[:OVERVIEW_TOP]],
        "upcoming": dated[:OVERVIEW_UPCOMING],
        "trend": trend,
        "by_project": per_project,
        # In the app's own order — quick wins first, thankless last — rather
        # than by size, so the four bars stay in the same places from one day
        # to the next and the shape is the thing that changed.
        "by_quadrant": [per_quadrant[key] for key in
                        ("quick_win", "major_project", "fill_in", "thankless",
                         "none")],
        "capacity": core.capacity(settings),
        "xp": core.xp_state(settings, ratios),
    }


@router.get("/api/overview")
def get_overview():
    """The Overview tab's numbers, graphs and shortlists.

    One payload like the calendar's, and fetched the same way: only while the
    tab is actually up. Everything in it spans every project, so unlike
    `/api/state` there is no open tab to ask about.
    """
    return _overview()
