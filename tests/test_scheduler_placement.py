"""Whole plans, recorded and compared: the regression net for #34.

Each scenario here is a complaint from the Scheduler Rework milestone, written
out as the smallest list of tasks that reproduces it, and each one is checked
against a recorded dump in `tests/golden/`.

The goldens record what the scheduler **does**, not what it should do. Several
of them are wrong on purpose: 04:00 blocks, twelve tasks stacked on one
instant, a fortnight of a daily chore that never appears. That is the point.
The issues in this milestone change them one at a time, and the golden diff in
each pull request is the evidence that the fix did what it said and nothing
else moved.

Rewrite a golden deliberately, never by hand:

    ADDERALL_UPDATE_GOLDEN=1 pytest tests/test_scheduler_placement.py
"""

import importlib
import os
from datetime import timedelta
from pathlib import Path

import pytest

from tests.planhelp import NOW, SETTINGS, WEEKDAYS, at, dump, task

GOLDEN = Path(__file__).parent / "golden"


def check(name: str, text: str, outside_window: bool = False) -> None:
    """Compare a dump against its recorded copy, or record it.

    Every dump is also held to the invariant this milestone exists for: no
    block outside the hours the settings say you work. The dump marks those
    itself (`<<<` and `>>>`), so the check is the marks being absent. Pass
    `outside_window=True` for a scenario where the user put the work there
    themselves, which is the only way it is allowed to happen.
    """
    if not outside_window:
        stray = [line for line in text.splitlines() if "<<<" in line or ">>>" in line]
        assert not stray, (
            f"{name} put work outside the working window:\n" + "\n".join(stray)
        )
    path = GOLDEN / f"{name}.txt"
    if os.environ.get("ADDERALL_UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text)
        return
    assert path.exists(), (
        f"no golden for {name}. If this scenario is new, record it with "
        f"ADDERALL_UPDATE_GOLDEN=1 and read the file before committing it."
    )
    assert text == path.read_text(), (
        f"the plan for {name} changed. Read the diff: if the new plan is the "
        f"one you meant to produce, re-record with ADDERALL_UPDATE_GOLDEN=1."
    )


# ---------------------------------------------------------------------------
# Pure placement: a task list in, a plan out
# ---------------------------------------------------------------------------

def test_four_hour_tree_due_at_eight_in_the_morning():
    """#36: a deadline you set is booked backwards from it, into the night.

    192 minutes of work is 240 minutes of block at the floor buffer, and the
    deadline is 08:00, so the block starts at 04:00 and the day view draws it
    there. This is the scenario the issue is named after.
    """
    tasks = [
        task("report", title="ship the quarterly report",
             deadline=at(31, 8), estimated_time=192, impact=8, effort=6),
    ]
    check("deadline_at_0800", dump(tasks, SETTINGS))


def test_fifteen_tasks_braindumped_in_one_minute():
    """The pile the day planner was written for: no dates, one created_at."""
    titles = [
        "call dentist", "renew passport", "fix the tap", "reply to Sam",
        "book MOT", "write up notes", "clear inbox", "order cat food",
        "chase invoice", "back up laptop", "sort photos", "cancel gym",
        "read the report", "plan the week", "tidy desk",
    ]
    tasks = [
        task(f"t{i}", title=title, estimated_time=30 + (i % 4) * 30,
             impact=(i * 3) % 11, effort=(i * 7) % 11, order_index=i)
        for i, title in enumerate(titles)
    ]
    check("braindump_fifteen", dump(tasks, SETTINGS))


def test_a_root_with_six_steps_and_a_deadline_three_days_out():
    """#35: every step gets a deadline of its own, tiled backwards.

    Six two-hour steps under a parent due at 17:00 are given 17:00, 15:00,
    13:00 and so on, walking straight off the start of the working day: the
    tree is twelve hours long, so it begins at 05:00.
    """
    tasks = [task("launch", title="launch the thing", deadline=at(31, 17),
                  impact=9, effort=7)]
    tasks += [
        task(f"step{i}", title=f"step {i}", parent_id="launch",
             estimated_time=96, order_index=i)
        for i in range(1, 7)
    ]
    check("tree_of_six_steps", dump(tasks, SETTINGS))


def test_a_start_time_in_the_evening_reopens_the_evening():
    """#36 again, from the other side: an explicit start time is honoured,
    and the stretched window it opens is then available to everything else
    placed on that day."""
    tasks = [
        task("dinner", title="cook dinner", start_at=at(29, 18),
             estimated_time=48, impact=6, effort=2),
        task("game", title="play the game", estimated_time=96,
             impact=2, effort=1, order_index=1),
        task("email", title="answer the email", estimated_time=24,
             impact=7, effort=2, order_index=2),
    ]
    check("evening_start_time", dump(tasks, SETTINGS))


def test_seventy_nine_hours_due_before_they_can_possibly_fit():
    """#65: more work than there is room for before the deadline.

    79 hours of work due on Tuesday, from a Saturday lunchtime. Every working
    hour between now and then adds up to 45, so 54 of them have nowhere legal
    to go. `_lay_back` used to answer that by putting the whole remainder in
    one span immediately before the earliest piece it had placed, which is how
    a plan ends up with a fifty-three hour block running through three nights.

    What it should say is the truth: here is the work that fits, in the hours
    you keep, and the rest does not.
    """
    tasks = [
        task("migration", title="migrate the database",
             deadline=at(1, 18, month=9), estimated_time=79 * 60,
             impact=9, effort=9),
    ]
    check("oversized_past_deadline", dump(tasks, SETTINGS))


def test_flexibility_decides_who_gets_the_contested_day():
    """Three tasks, one day, same everything but how movable they are.

    The one that cannot move gets the morning, the ordinary one takes what is
    left of the day, and the filler goes wherever there is room after that.
    """
    when = at(31, 9)
    tasks = [
        task("filler", title="read the thing", start_at=when,
             estimated_time=192, impact=5, effort=5, flexibility=5,
             order_index=0),
        task("normal", title="write the notes", start_at=when,
             estimated_time=192, impact=5, effort=5, flexibility=3,
             order_index=1),
        task("fixed", title="the standup", start_at=when,
             estimated_time=192, impact=5, effort=5, flexibility=1,
             order_index=2),
    ]
    check("flexibility_contested_day", dump(tasks, SETTINGS))


# ---------------------------------------------------------------------------
# The two that need a database
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_db(monkeypatch, tmp_path):
    """A temp database and a client on it, the way `test_api.py` does it."""
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "plan.db"))
    from app import db
    importlib.reload(db)
    from app import main, recurring
    importlib.reload(recurring)
    importlib.reload(main)
    db.update_settings(SETTINGS)
    return db, main, recurring


def test_a_daily_chore_left_alone_for_a_fortnight(app_db):
    """#35: the missed copy stays open, and blocks every later beat."""
    db, _main, recurring = app_db
    project = db.ensure_project()
    chore = db.create_task({"title": "put the bins out", "project_id": project["id"],
                            "deadline": at(29, 9), "estimated_time": 20})
    series = recurring.start_series(chore, {"freq": "daily", "interval": 1},
                                    now=NOW)

    for day in range(1, 15):                 # a fortnight of nobody touching it
        recurring.sweep(now=NOW + timedelta(days=day))

    rows = sorted(db.list_tasks(), key=lambda t: (t["deadline"] or "", t["id"]))
    series = db.get_series(series["id"])
    lines = [f"copies on the list: {len(rows)}"]
    lines += [f"  {r['deadline']}  {r['status']:12}  {r['title']}" for r in rows]
    lines.append(f"series next_at: {series['next_at']}")
    lines.append(f"series active: {series['active']}  made: {series['made']}")
    lines.append(f"still has an open copy: {db.has_open_occurrence(series['id'])}")
    check("repeat_missed_fortnight", "\n".join(lines) + "\n")


def _repeat_plan(db, recurring, when: str | None) -> str:
    """A weekday rhythm's next fortnight, as the day book actually holds it.

    Driven off `recurring.forecast` and `reserve_forecast` rather than the
    calendar endpoint, because that endpoint reads the wall clock and a golden
    file that moves with the calendar is not a golden file. It is the same
    placement path either way: the endpoint books the forecast through exactly
    these two calls.
    """
    from app import logic
    project = db.ensure_project()
    job = db.create_task({"title": "work", "project_id": project["id"],
                          "estimated_time": 480})
    rule = {"freq": "weekly", "interval": 1, "weekdays": [1, 2, 3, 4, 5]}
    if when:
        rule["time"] = when
    recurring.start_series(job, rule, now=NOW)

    tz = logic.resolve_tz(SETTINGS["timezone"])
    occurrences = recurring.forecast(now=NOW, settings=SETTINGS, days=14)
    planner = logic.day_planner(SETTINGS, now=NOW)
    recurring.reserve_forecast(planner, occurrences)

    planted = db.get_task(job["id"])
    lines = ["work, 8h, every weekday" + (f" at {when}" if when else ""),
             f"the copy on the list is due {planted['deadline']}"
             + (f", starting {planted['start_at']}" if planted["start_at"] else ""),
             ""]
    for occ in occurrences:
        due = (occ.get("due_at") or occ["at"]).astimezone(tz)
        opens = occ.get("start_at")
        lines.append(f"due {WEEKDAYS[due.weekday()]} {due:%-d %b %H:%M}"
                     + (f"  starting {opens.astimezone(tz):%H:%M}" if opens else ""))
        for a, b in planner.spans(occ["key"]):
            a, b = a.astimezone(tz), b.astimezone(tz)
            note = "" if a.date() == due.date() else "   (not the day it is due)"
            lines.append(f"  {WEEKDAYS[a.weekday()]} {a:%-d %b %H:%M}-{b:%H:%M}{note}")
    return "\n".join(lines) + "\n"


def test_a_weekday_rhythm_at_nine_is_worked_the_day_before(app_db):
    """#63/#66: "work, every weekday, at 09:00" put Monday's shift on Sunday.

    The repeat dialog labels that field **At**, so it is the hour the job
    happens. It was stored as the hour the job is *due*, and `_lay_back` then
    laid eight hours of it backwards out of Monday and into Sunday — a day the
    rule does not even name.
    """
    db, _main, recurring = app_db
    check("repeat_at_nine", _repeat_plan(db, recurring, "09:00"))


def test_a_weekday_rhythm_in_the_afternoon_is_worked_the_day_before_too(app_db):
    """The same defect, less obviously: an afternoon hour leaves some room on
    its own day, so only the overflow lands on the day before. It is the hour
    being read as a deadline that does it, not the hour being early."""
    db, _main, recurring = app_db
    check("repeat_at_five", _repeat_plan(db, recurring, "17:00"))


def test_a_weekday_rhythm_with_no_hour_named_is_unchanged(app_db):
    """The control. A rhythm naming no time already lands on its own day, via
    `recurring._end_of_working_day`, and must keep doing so."""
    db, _main, recurring = app_db
    check("repeat_no_hour", _repeat_plan(db, recurring, None))


def test_a_dozen_overdue_tasks_cleared_onto_the_days_that_fit(app_db):
    """#35: the pile, rescheduled.

    "Reschedule all" used to write one instant onto every task, which is the
    same pile on a different day. It goes through the day book now, in score
    order, and comes back as a spread.
    """
    from app import logic

    db, _main, _recurring = app_db
    project = db.ensure_project()
    for i in range(12):
        db.create_task({"title": f"overdue {i + 1}", "project_id": project["id"],
                        "deadline": at(20 + i % 5, 9 + i % 6),
                        "estimated_time": 48, "order_index": i,
                        "impact": (i * 3) % 11, "effort": (i * 7) % 11})

    # What `/api/reschedule` does, against this file's fixed clock rather than
    # the real one — a golden that moves with the calendar is not a golden.
    tasks = db.list_tasks()
    moves = logic.reschedule_plan(tasks, db.get_settings(),
                                  [t["id"] for t in tasks], "tomorrow", now=NOW)
    for move in moves:
        db.update_task(move["task_id"], {"deadline": move["deadline"]})

    check("bulk_reschedule_twelve", dump(db.list_tasks(), db.get_settings(), NOW))
