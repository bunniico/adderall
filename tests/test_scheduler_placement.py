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

from tests.planhelp import NOW, SETTINGS, at, dump, task

GOLDEN = Path(__file__).parent / "golden"


def check(name: str, text: str) -> None:
    """Compare a dump against its recorded copy, or record it."""
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


def test_a_dozen_overdue_tasks_nudged_to_tomorrow(app_db):
    """#35: "reschedule all" writes one instant onto every task in the pile."""
    from fastapi.testclient import TestClient

    db, main, _recurring = app_db
    client = TestClient(main.app)
    project = db.ensure_project()
    for i in range(12):
        db.create_task({"title": f"overdue {i + 1}", "project_id": project["id"],
                        "deadline": at(20 + i % 5, 9 + i % 6),
                        "estimated_time": 48, "order_index": i})

    tomorrow = at(30, 9)                     # what "tomorrow morning" resolves to
    response = client.post("/api/nudge", json={"nudges": [
        {"task_id": t["id"], "deadline": tomorrow} for t in db.list_tasks()
    ]})
    assert response.status_code == 200

    check("bulk_nudge_twelve", dump(db.list_tasks(), db.get_settings(), NOW))
