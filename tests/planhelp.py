"""A whole schedule, rendered as text you can read in one go.

Every other test in this suite asks the scheduler one question and checks one
answer. That is the right shape for `next_occurrence` and the wrong shape for
placement, where the bug is never in one block: it is in where the fourth one
landed once the first three had taken their days. Reading that back one
assertion at a time is how a plan full of 4am blocks passed a green suite.

So this renders the plan. Days down the page, blocks inside them, the load
against the cap on every day header, and a mark on anything sitting outside
the working window. A scenario plus its recorded dump is a regression test you
can review by reading the diff, which is the point: when a change moves work,
the golden file says exactly where it moved it to.

The dump is the app's own maths, not a second implementation of it. Blocks are
`logic.compute` output, drawn the way the day view draws them (`length_min`
back from the deadline, see `app/static/calendar.js`), so the picture here is
the picture on screen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import logic

# A fixed clock, because a golden file that moves with the calendar is not a
# golden file. A Saturday, deliberately: nothing here should depend on it, and
# a weekday assumption that creeps in shows up as a diff.
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

# The base every scenario starts from. Buffer is pinned at the floor so an
# estimate in a scenario reads as the number it is: 192 minutes of work is 240
# minutes of block, and 4 hours is recognisably 4 hours in the dump.
SETTINGS = {
    "buffer": 0.25,
    "adaptive_buffer": False,
    "adaptive_capacity": False,
    "matrix_threshold": 5,
    "auto_deadlines": True,
    "spread_tasks": True,
    "day_capacity": 480,
    "day_start": 9,
    "timezone": "UTC",
}

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def task(id, **kw) -> dict:
    """One task row, with every column `logic.compute` reads."""
    row = {
        "id": id, "title": id, "description": "", "parent_id": None,
        "project_id": "p1", "deadline": None, "start_at": None,
        "estimated_time": None, "actual_time": None,
        "impact": None, "effort": None, "status": "todo",
        "ack_thankless": False, "collapsed": False, "repeat_carry": True,
        "order_index": 0, "started_at": None, "xp_awarded": None,
        "series_id": None, "clickup_id": None,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    row.update(kw)
    return row


def at(day: int, hour: int = 9, minute: int = 0, month: int = 8) -> str:
    """An instant in the scenarios' own August, as the ISO string a row holds."""
    return datetime(2026, month, day, hour, minute,
                    tzinfo=timezone.utc).isoformat(timespec="seconds")


def _minutes(total: int) -> str:
    hours, mins = divmod(int(total), 60)
    if not hours:
        return f"{mins}m"
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def blocks(tasks: list[dict], settings: dict, now: datetime | None = None,
           ratios: list[float] | None = None) -> list[dict]:
    """What the calendar would draw: one block per scheduled task.

    A block ends at the task's deadline and starts `length_min` earlier, which
    is where the hours outside the working day become visible.
    """
    now = now or NOW
    derived = logic.compute(tasks, settings, ratios or [], now=now)
    tz = logic.resolve_tz(settings.get("timezone"))
    out: list[dict] = []
    for t in tasks:
        d = derived[t["id"]]
        deadline = logic.parse_dt(d.get("deadline"))
        if deadline is None:
            continue
        length = d["length_min"]
        end = deadline.astimezone(tz)
        out.append({
            "id": t["id"], "title": t["title"],
            "start": end - timedelta(minutes=length), "end": end,
            "length": length, "source": d["deadline_source"],
            # Containers are drawn, but their minutes are their steps' minutes,
            # so counting both would charge every day twice.
            "counts": not d["has_subtasks"],
            "depth": len(d["order_path"]) - 1,
        })
    out.sort(key=lambda b: (b["start"], b["title"]))
    return out


def dump(tasks: list[dict], settings: dict | None = None,
         now: datetime | None = None, ratios: list[float] | None = None) -> str:
    """The whole plan as text: the settings that produced it, then the days."""
    settings = settings if settings is not None else SETTINGS
    now = now or NOW
    tz = logic.resolve_tz(settings.get("timezone"))
    cap = logic.capacity_plan(settings)["minutes"]
    day_start = int(settings.get("day_start", logic.DEFAULT_DAY_START)) * 60
    window_end = min(24 * 60, day_start + cap)

    lines = [
        f"now {now.astimezone(tz):%Y-%m-%d %H:%M} {settings.get('timezone', 'UTC')}",
        f"day {day_start // 60:02d}:00 to {window_end // 60:02d}:{window_end % 60:02d}"
        f" | cap {_minutes(cap)} | buffer {int(settings.get('buffer', 0.3) * 100)}%",
        "",
    ]

    drawn = blocks(tasks, settings, now, ratios)
    by_day: dict = {}
    for block in drawn:
        by_day.setdefault(block["start"].date(), []).append(block)

    previous = None
    for day in sorted(by_day):
        if previous is not None and (day - previous).days > 1:
            gap = (day - previous).days - 1
            lines.append(f"  ... {gap} day{'' if gap == 1 else 's'} with nothing ...")
        previous = day
        load = sum(b["length"] for b in by_day[day] if b["counts"])
        over = "  OVER CAP" if load > cap else ""
        lines.append(f"{day:%Y-%m-%d} {WEEKDAYS[day.weekday()]}  "
                     f"load {_minutes(load)} / {_minutes(cap)}{over}")
        for block in by_day[day]:
            start_min = block["start"].hour * 60 + block["start"].minute
            end_min = block["end"].hour * 60 + block["end"].minute
            spill = "+1" if block["end"].date() != block["start"].date() else "  "
            # The mark this file exists for: work the app put outside the hours
            # the settings say you work.
            outside = ("  <<< before the day starts" if start_min < day_start else
                       "  >>> after the day ends"
                       if (spill == "+1" or end_min > window_end) else "")
            indent = "  " * block["depth"]
            lines.append(
                f"  {block['start']:%H:%M}-{block['end']:%H:%M}{spill} "
                f"{_minutes(block['length']):>6}  {indent}{block['title']}"
                f"  [{block['source']}]{outside}")

    unscheduled = sorted(t["title"] for t in tasks
                         if t["id"] not in {b["id"] for b in drawn})
    if unscheduled:
        lines.append("")
        lines.append("nothing scheduled:")
        lines.extend(f"  {title}" for title in unscheduled)
    return "\n".join(lines) + "\n"
