"""Recurring tasks: the date arithmetic, and what the app does with it.

The first half is pure `logic` — a rule in, the next instant out — and needs
no database. The second half drives the API the way the page does, with the AI
stubbed out and the background sweep called by hand so the tests own the clock.
"""

import importlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import logic
from app import recurring as recurrence

NY = ZoneInfo("America/New_York")


def when(rule, after, anchor=None, tz=NY):
    got = logic.next_occurrence(rule, after, anchor=anchor or after, tz=tz)
    return got.astimezone(tz) if got else None


def local(y, m, d, hh=9, mm=0, tz=NY):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


# ---------------------------------------------------------------------------
# normalizing a rule
# ---------------------------------------------------------------------------

def test_a_rule_without_a_frequency_is_not_a_rule():
    assert logic.normalize_rule(None) is None
    assert logic.normalize_rule({}) is None
    assert logic.normalize_rule({"freq": "fortnightly"}) is None
    assert logic.normalize_rule("daily") is None


def test_normalize_clamps_rather_than_raises():
    """A rule the app cannot use must never be a rule that will not save."""
    rule = logic.normalize_rule({
        "freq": "WEEKLY", "interval": 99999, "weekdays": [0, 9, "3", 3, -1],
        "month_day": 40, "nth": 12, "weekday": 77, "time": "26:70",
        "count": -5, "until": "not a date", "lead_days": 900,
    })
    assert rule["freq"] == "weekly"
    assert rule["interval"] == logic.RECUR_MAX_INTERVAL
    assert rule["weekdays"] == [0, 3]      # out of range and duplicates dropped
    assert rule["month_day"] is None      # out of range, and weekly anyway
    assert rule["nth"] is None
    assert rule["weekday"] is None
    assert rule["time"] is None
    assert rule["count"] == 1
    assert rule["until"] is None
    assert rule["lead_days"] == logic.RECUR_MAX_LEAD_DAYS


def test_a_rule_only_carries_the_fields_its_frequency_uses():
    """The dialog sends every control it has; the rule keeps only the ones
    this frequency means, so a stored weekly rule cannot come back and
    repopulate the monthly controls with an answer nobody gave."""
    assert logic.normalize_rule({"freq": "monthly", "weekdays": [1, 2]})["weekdays"] == []
    weekly = logic.normalize_rule({"freq": "weekly", "weekdays": [2],
                                   "monthly_mode": "nth_weekday", "nth": 3,
                                   "weekday": 4, "month_day": 9})
    assert weekly["monthly_mode"] == "day_of_month"
    assert (weekly["month_day"], weekly["nth"], weekly["weekday"]) == (None, None, None)
    nth = logic.normalize_rule({"freq": "monthly", "monthly_mode": "nth_weekday",
                                "nth": 3, "weekday": 4, "month_day": 9})
    assert nth["month_day"] is None and nth["nth"] == 3
    dom = logic.normalize_rule({"freq": "monthly", "month_day": 9,
                                "nth": 3, "weekday": 4})
    assert dom["month_day"] == 9 and (dom["nth"], dom["weekday"]) == (None, None)


def test_last_day_and_last_weekday_survive_normalizing():
    rule = logic.normalize_rule({"freq": "monthly", "month_day": -1})
    assert rule["month_day"] == -1
    rule = logic.normalize_rule({"freq": "monthly", "monthly_mode": "nth_weekday",
                                 "nth": -1, "weekday": 5})
    assert (rule["nth"], rule["weekday"]) == (-1, 5)


# ---------------------------------------------------------------------------
# daily
# ---------------------------------------------------------------------------

def test_daily_is_the_next_day():
    start = local(2026, 3, 2)
    assert when({"freq": "daily"}, start) == local(2026, 3, 3)


def test_every_n_days_keeps_its_phase():
    """Each answer is fed back in; the rhythm must not drift off its anchor."""
    anchor = local(2026, 5, 1, 7, 30)
    cursor, seen = anchor, []
    for _ in range(3):
        cursor = when({"freq": "daily", "interval": 3}, cursor, anchor=anchor)
        seen.append(cursor.date())
    assert seen == [local(2026, 5, 4).date(), local(2026, 5, 7).date(),
                    local(2026, 5, 10).date()]


def test_a_fixed_time_of_day_wins_over_the_anchors():
    got = when({"freq": "daily", "time": "06:15"}, local(2026, 3, 2, 22, 40))
    assert (got.hour, got.minute) == (6, 15)


def test_no_time_of_day_keeps_the_anchors():
    got = when({"freq": "daily"}, local(2026, 3, 2, 22, 40))
    assert (got.hour, got.minute) == (22, 40)


def test_the_wall_clock_survives_a_dst_change():
    """A 9am chore is a 9am chore in March too — the local hour is the promise,
    the UTC instant is just how it is stored."""
    anchor = local(2026, 3, 6, 9, 0)   # US clocks go forward on 8 March 2026
    cursor = anchor
    for _ in range(4):
        cursor = when({"freq": "daily"}, cursor, anchor=anchor)
        assert (cursor.hour, cursor.minute) == (9, 0)
    assert cursor.date() == local(2026, 3, 10).date()


# ---------------------------------------------------------------------------
# weekly
# ---------------------------------------------------------------------------

def test_weekly_without_days_repeats_the_anchors_day():
    start = local(2026, 3, 3)          # a Tuesday
    assert when({"freq": "weekly"}, start).date() == local(2026, 3, 10).date()


def test_weekly_walks_the_chosen_days():
    anchor = local(2026, 3, 2)         # Monday
    rule = {"freq": "weekly", "weekdays": [1, 3, 5]}   # Mon, Wed, Fri
    cursor, seen = anchor, []
    for _ in range(4):
        cursor = when(rule, cursor, anchor=anchor)
        seen.append(cursor.date().isoformat())
    assert seen == ["2026-03-04", "2026-03-06", "2026-03-09", "2026-03-11"]


def test_every_other_week_skips_the_week_between():
    anchor = local(2026, 3, 3)         # Tuesday
    rule = {"freq": "weekly", "interval": 2, "weekdays": [2, 4]}  # Tue & Thu
    cursor, seen = anchor, []
    for _ in range(4):
        cursor = when(rule, cursor, anchor=anchor)
        seen.append(cursor.date().isoformat())
    # Same week's Thursday, then nothing at all in the week after.
    assert seen == ["2026-03-05", "2026-03-17", "2026-03-19", "2026-03-31"]


def test_the_fortnight_grid_is_not_the_calendars_week_start():
    """Sat and Sun are in the same fortnightly beat as the Tue before them.

    Which day a *calendar* starts on is a display preference; letting it shift
    a repeat would mean the same rule meant two different things on two
    machines.
    """
    anchor = local(2026, 3, 3)         # Tuesday
    rule = {"freq": "weekly", "interval": 2, "weekdays": [0, 6]}  # Sun & Sat
    got = when(rule, anchor, anchor=anchor)
    assert got.date().isoformat() == "2026-03-07"   # the Saturday of that week


# ---------------------------------------------------------------------------
# monthly and yearly
# ---------------------------------------------------------------------------

def test_monthly_repeats_the_anchors_day_of_month():
    anchor = local(2026, 1, 15)
    assert when({"freq": "monthly"}, anchor).date().isoformat() == "2026-02-15"


def test_the_31st_clamps_into_short_months_and_comes_back():
    """"Monthly on the 31st" is a bill at the end of every month, February
    included — clamped, not skipped, and not permanently moved to the 28th."""
    anchor = local(2026, 1, 31, 8, 0)
    rule = {"freq": "monthly", "month_day": 31}
    cursor, seen = anchor, []
    for _ in range(4):
        cursor = when(rule, cursor, anchor=anchor)
        seen.append(cursor.date().isoformat())
    assert seen == ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]


def test_minus_one_is_the_last_day_whatever_length_the_month_is():
    anchor = local(2026, 1, 31)
    rule = {"freq": "monthly", "month_day": -1}
    cursor, seen = anchor, []
    for _ in range(3):
        cursor = when(rule, cursor, anchor=anchor)
        seen.append(cursor.date().isoformat())
    assert seen == ["2026-02-28", "2026-03-31", "2026-04-30"]


def test_the_last_friday_of_every_month():
    anchor = local(2026, 1, 30, 17, 0)
    rule = {"freq": "monthly", "monthly_mode": "nth_weekday", "nth": -1, "weekday": 5}
    cursor, seen = anchor, []
    for _ in range(3):
        cursor = when(rule, cursor, anchor=anchor)
        seen.append(cursor.date().isoformat())
    assert seen == ["2026-02-27", "2026-03-27", "2026-04-24"]


def test_the_second_tuesday_of_every_third_month():
    anchor = local(2026, 1, 13)
    rule = {"freq": "monthly", "interval": 3, "monthly_mode": "nth_weekday",
            "nth": 2, "weekday": 2}
    cursor, seen = anchor, []
    for _ in range(2):
        cursor = when(rule, cursor, anchor=anchor)
        seen.append(cursor.date().isoformat())
    assert seen == ["2026-04-14", "2026-07-14"]


def test_a_month_with_no_fifth_friday_is_a_month_this_rule_skips():
    anchor = local(2026, 1, 30)        # January 2026 has five Fridays
    rule = {"freq": "monthly", "monthly_mode": "nth_weekday", "nth": 5, "weekday": 5}
    got = when(rule, anchor, anchor=anchor)
    # February, March and April 2026 have four; May has five.
    assert got.date().isoformat() == "2026-05-29"


def test_yearly_is_twelve_months_of_the_same_walk():
    anchor = local(2026, 3, 14)
    assert when({"freq": "yearly"}, anchor).date().isoformat() == "2027-03-14"
    assert when({"freq": "yearly", "interval": 2}, anchor).date().isoformat() == "2028-03-14"


def test_the_29th_of_february_lands_on_the_28th_in_common_years():
    anchor = local(2024, 2, 29)
    got = when({"freq": "yearly"}, anchor, anchor=anchor)
    assert got.date().isoformat() == "2025-02-28"


# ---------------------------------------------------------------------------
# when a rhythm stops
# ---------------------------------------------------------------------------

def test_until_ends_the_rule():
    anchor = local(2026, 5, 1)
    rule = {"freq": "daily", "until": "2026-05-03"}
    first = when(rule, anchor, anchor=anchor)
    second = when(rule, first, anchor=anchor)
    assert [first.date().isoformat(), second.date().isoformat()] == \
        ["2026-05-02", "2026-05-03"]
    assert when(rule, second, anchor=anchor) is None


def test_until_on_a_monthly_rule_stops_it_too():
    anchor = local(2026, 1, 15)
    rule = {"freq": "monthly", "until": "2026-02-20"}
    assert when(rule, anchor, anchor=anchor).date().isoformat() == "2026-02-15"
    assert when(rule, local(2026, 2, 15), anchor=anchor) is None


# ---------------------------------------------------------------------------
# saying it out loud
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule, expected", [
    ({"freq": "daily"}, "every day"),
    ({"freq": "daily", "interval": 3}, "every 3 days"),
    ({"freq": "weekly", "weekdays": [1, 3, 5]}, "every week on Mon, Wed & Fri"),
    ({"freq": "weekly", "interval": 2, "weekdays": [2]}, "every 2 weeks on Tue"),
    ({"freq": "monthly", "month_day": 1}, "every month on the 1st"),
    ({"freq": "monthly", "month_day": -1}, "every month on the last"),
    ({"freq": "monthly", "interval": 2, "monthly_mode": "nth_weekday",
      "nth": 3, "weekday": 4}, "every 2 months on the 3rd Thu"),
])
def test_describe_says_what_will_happen(rule, expected):
    assert logic.describe_rule(rule, local(2026, 3, 3), NY) == expected


def test_describe_fills_in_what_the_rule_leaves_implicit():
    """A monthly rule with no day set repeats on the day the series started,
    so the sentence has to say which day that is."""
    assert logic.describe_rule({"freq": "monthly"}, local(2026, 3, 17), NY) == \
        "every month on the 17th"
    assert logic.describe_rule({"freq": "weekly"}, local(2026, 3, 3), NY) == \
        "every week on Tue"


def test_describe_carries_the_trimmings():
    got = logic.describe_rule(
        {"freq": "daily", "time": "07:30", "count": 5, "from_completion": True},
        local(2026, 3, 3), NY)
    assert got == ("every day · at 07:30 · counted from when you finish it "
                   "· 5 times")


def test_describe_of_a_non_rule_is_nothing_at_all():
    assert logic.describe_rule(None) == ""
    assert logic.describe_rule({"freq": "whenever"}) == ""


# ---------------------------------------------------------------------------
# the app: series, occurrences and the sweep
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    # The background sweep is the one thing these tests must own the clock of.
    monkeypatch.setenv("ADDERALL_RECUR_INTERVAL", "0")
    from app import db
    importlib.reload(db)
    from app import main, recurring
    importlib.reload(recurring)
    importlib.reload(main)
    monkeypatch.setattr(main.ai, "annotate",
                        lambda settings, tasks, want_scores=True: {})
    monkeypatch.setattr(main.ai, "extract_schedule",
                        lambda settings, title, now_local: {
                            "has_deadline": False, "deadline_in_minutes": 0,
                            "has_repeat": False, "repeat_freq": "",
                            "repeat_interval": 1, "repeat_weekdays": [],
                            "clean_title": title,
                        })
    client = TestClient(main.app)
    client.put("/api/settings", json={"timezone": "UTC"})
    return client, main, db, recurring


def add(client, title="take the bins out", **kw):
    body = {"title": title, "annotate": False, "estimated_time": 10, **kw}
    res = client.post("/api/tasks", json=body)
    assert res.status_code == 201, res.text
    return next(t for t in res.json()["tasks"] if t["title"] == title)


def repeat(client, task_id, **rule):
    res = client.put(f"/api/tasks/{task_id}/repeat", json={"freq": "daily", **rule})
    assert res.status_code == 200, res.text
    return res.json()


def advance(db, recurring, minutes=1):
    """Let the clock reach the series' next occurrence and sweep.

    Occurrences land at the end of the working day when a rule names no time,
    so how soon after finishing one the next copy appears depends on what time
    of day the suite happens to run. Tests that care about *what* the sweep
    produces read the date off the series rather than guessing at it.
    """
    series = db.list_series()
    if not series or not series[0]["next_at"]:
        return
    recurring.sweep(logic.parse_dt(series[0]["next_at"]) + timedelta(minutes=minutes))


def roots(state, title=None):
    tasks = state["tasks"]
    return [t for t in tasks if title is None or t["title"] == title]


def test_setting_a_repeat_dates_the_task_and_describes_itself(app):
    client, *_ = app
    task = add(client)
    state = repeat(client, task["id"], freq="weekly", weekdays=[2], time="19:00")
    got = roots(state)[0]
    assert got["recurrence"]["summary"] == "every week on Tue · at 19:00"
    assert got["recurrence"]["active"] is True
    # An undated task gets the first occurrence as its deadline: saying "every
    # Tuesday" about something with no date is how you give it a Tuesday.
    assert got["deadline_source"] == "user"
    assert logic.parse_dt(got["deadline"]).weekday() == 1   # Python's Tuesday
    assert got["deadline"] == got["recurrence"]["anchor_at"]


def test_an_undated_task_is_due_by_the_end_of_the_working_day(app):
    """A chore with no time on it must not arrive already overdue.

    "Every 3 days", set at two in the morning, does not mean two in the
    morning — it means by the end of the day, which is a number the app
    already keeps as the working window.
    """
    client, *_ = app
    client.put("/api/settings", json={"day_start": 9, "day_capacity": 480,
                                      "adaptive_capacity": False})
    task = add(client)
    state = repeat(client, task["id"], freq="daily", interval=3)
    due = logic.parse_dt(roots(state)[0]["deadline"])
    assert due > datetime.now(timezone.utc)
    assert (due.hour, due.minute) == (17, 0)      # 09:00 + eight hours, in UTC


def test_a_named_time_of_day_beats_the_working_window(app):
    client, *_ = app
    task = add(client)
    state = repeat(client, task["id"], freq="daily", time="07:30")
    due = logic.parse_dt(roots(state)[0]["deadline"])
    assert (due.hour, due.minute) == (7, 30)


def test_a_task_that_already_has_a_deadline_keeps_it_as_the_first_beat(app):
    client, *_ = app
    due = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    task = add(client, deadline=due.isoformat())
    state = repeat(client, task["id"], freq="weekly")
    got = roots(state)[0]
    assert logic.parse_dt(got["deadline"]) == due
    # Occurrences land on whole minutes — a repeat is a time of day, not a
    # stopwatch reading — so the week later is measured from the same minute.
    following = logic.parse_dt(got["recurrence"]["next_at"])
    assert following == due.replace(second=0) + timedelta(days=7)


def test_finishing_one_occurrence_opens_the_next(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    advance(db, recurring)
    state = client.get("/api/state").json()
    titles = roots(state, "take the bins out")
    assert sorted(t["status"] for t in titles) == ["done", "todo"]
    fresh = next(t for t in titles if t["status"] == "todo")
    assert fresh["id"] != task["id"]
    assert fresh["recurrence"]["made"] == 2
    # The copy is a fresh task, not the old one reopened: no XP, no timer.
    assert fresh["xp_awarded"] is None
    assert fresh["actual_time"] is None
    assert fresh["started_at"] is None


def test_only_one_copy_is_ever_open_at_a_time(app):
    """A fortnight away from a daily chore leaves one thing to do, not fourteen."""
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    now = datetime.now(timezone.utc)
    for day in range(1, 15):
        recurring.sweep(now + timedelta(days=day))
    open_now = [t for t in db.list_tasks() if t["status"] == "todo"]
    assert len(open_now) == 1
    assert open_now[0]["id"] == task["id"]
    # ...and the rhythm has moved on rather than staying stuck in the past.
    series = db.list_series()[0]
    assert logic.parse_dt(series["next_at"]) > now + timedelta(days=14)


def test_the_sweep_makes_the_copy_when_the_day_comes(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily", lead_days=0)
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    assert len(db.list_tasks()) == 1          # tomorrow is not today
    # A lead of none means "on the day it is due", so any moment still on the
    # day before leaves the list alone. Measured against the occurrence rather
    # than against a fixed number of hours, because a lead is counted in days.
    due = logic.parse_dt(db.list_series()[0]["next_at"])
    recurring.sweep(due - timedelta(days=1))
    assert len(db.list_tasks()) == 1          # ...and a day out, still not
    advance(db, recurring)
    tasks = db.list_tasks()
    assert len(tasks) == 2
    assert sorted(t["status"] for t in tasks) == ["done", "todo"]


def test_the_sweep_is_idempotent(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily", lead_days=0)
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    later = logic.parse_dt(db.list_series()[0]["next_at"]) + timedelta(minutes=1)
    for _ in range(5):
        recurring.sweep(later)
    assert len(db.list_tasks()) == 2


def test_the_sweep_route_runs_the_same_pass(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily", lead_days=0)
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    body = client.post("/api/recurring/run").json()
    assert body["recurring"]["created"] == []      # still not tomorrow
    assert "ran_at" in body["recurring"]
    assert "tasks" in body                         # and it hands back the page


def test_this_weeks_edits_are_next_weeks_starting_point(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    client.patch(f"/api/tasks/{task['id']}",
                 json={"title": "take the bins out (green one)",
                       "description": "kerb by 7", "estimated_time": 4})
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    advance(db, recurring)
    state = client.get("/api/state").json()
    fresh = next(t for t in state["tasks"] if t["status"] == "todo")
    assert fresh["title"] == "take the bins out (green one)"
    assert fresh["description"] == "kerb by 7"
    assert fresh["estimated_time"] == 4


def test_the_steps_come_back_with_it(app):
    client, main, db, recurring = app
    task = add(client, title="sunday reset")
    for step in ("strip the beds", "run the wash", "dropped step"):
        client.post("/api/tasks", json={"title": step, "parent_id": task["id"],
                                        "annotate": False})
    dropped = next(t for t in db.list_tasks() if t["title"] == "dropped step")
    client.patch(f"/api/tasks/{dropped['id']}", json={"status": "discarded"})
    repeat(client, task["id"], freq="weekly")
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    advance(db, recurring)
    state = client.get("/api/state").json()
    fresh = next(t for t in state["tasks"]
                 if t["title"] == "sunday reset" and t["status"] == "todo")
    steps = [s["title"] for s in fresh["subtasks"]]
    # A step you dropped stays dropped; the rest come back as fresh todos.
    assert steps == ["strip the beds", "run the wash"]
    assert all(s["status"] == "todo" for s in fresh["subtasks"])
    # Only the top of the tree belongs to the series; the steps are just steps.
    assert all(s["recurrence"] is None for s in fresh["subtasks"])


def test_counting_from_completion_starts_the_clock_when_you_finish(app):
    client, main, db, _ = app
    due = (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0)
    task = add(client, title="water the plants", deadline=due.isoformat())
    repeat(client, task["id"], freq="daily", interval=3, from_completion=True)
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    series = db.list_series()[0]
    # Three days from now, not three days from a deadline ten days gone.
    gap = logic.parse_dt(series["next_at"]) - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < gap < timedelta(days=3, hours=1)


def test_clearing_a_backlog_hands_you_the_next_one_not_last_months(app):
    client, main, db, _ = app
    due = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    task = add(client, title="pay the rent", deadline=due.isoformat())
    repeat(client, task["id"], freq="weekly")
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    series = db.list_series()[0]
    assert logic.parse_dt(series["next_at"]) > datetime.now(timezone.utc)


def test_discarding_a_copy_keeps_the_rhythm(app):
    client, main, db, _ = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    client.patch(f"/api/tasks/{task['id']}", json={"status": "discarded"})
    series = db.list_series()[0]
    assert series["active"] is True
    assert logic.parse_dt(series["next_at"]) > datetime.now(timezone.utc)


def test_deleting_a_copy_deletes_the_copy_not_the_job(app):
    client, main, db, _ = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    client.delete(f"/api/tasks/{task['id']}")
    assert db.get_task(task["id"]) is None
    series = db.list_series()
    assert len(series) == 1 and series[0]["active"] is True
    # The template outlived the task it was taken from, which is the whole
    # reason the series is not hung off the task row.
    assert series[0]["template"]["title"] == "take the bins out"


def test_stop_repeating_leaves_the_task_alone(app):
    client, main, db, _ = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    state = client.delete(f"/api/tasks/{task['id']}/repeat").json()
    got = roots(state)[0]
    assert got["recurrence"] is None
    assert got["status"] == "todo"          # still a real task you may do
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    assert len(db.list_tasks()) == 1        # and nothing follows it


def test_stopping_a_repeat_a_task_never_had_is_a_404(app):
    client, *_ = app
    task = add(client)
    assert client.delete(f"/api/tasks/{task['id']}/repeat").status_code == 404


def test_a_count_runs_the_series_out(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily", count=3, lead_days=1)
    for round_ in range(3):
        open_now = [t for t in db.list_tasks() if t["status"] == "todo"]
        assert len(open_now) == 1, f"round {round_}"
        client.post(f"/api/tasks/{open_now[0]['id']}/complete", json={})
        advance(db, recurring)
    assert [t["status"] for t in db.list_tasks()] == ["done"] * 3
    assert db.list_series(active_only=True) == []


def test_an_until_date_runs_the_series_out(app):
    client, main, db, _ = app
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    task = add(client)
    res = client.put(f"/api/tasks/{task['id']}/repeat",
                     json={"freq": "daily", "until": yesterday.isoformat()})
    assert res.status_code == 400        # nothing about it ever comes round
    assert db.list_series(active_only=False) == []


def test_changing_the_rule_keeps_the_series_and_its_history(app):
    client, main, db, _ = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    first = db.list_series()[0]
    state = repeat(client, task["id"], freq="weekly", interval=2, weekdays=[3])
    series = db.list_series()
    assert len(series) == 1 and series[0]["id"] == first["id"]
    assert roots(state)[0]["recurrence"]["summary"] == "every 2 weeks on Wed"


def test_only_top_level_tasks_repeat(app):
    client, *_ = app
    parent = add(client, title="move house")
    sub = client.post("/api/tasks", json={"title": "book the van",
                                          "parent_id": parent["id"],
                                          "annotate": False}).json()
    kid = next(t for t in sub["tasks"] if t["title"] == "move house")["subtasks"][0]
    res = client.put(f"/api/tasks/{kid['id']}/repeat", json={"freq": "daily"})
    assert res.status_code == 400
    assert "top-level" in res.json()["detail"]


def test_a_nonsense_rule_is_refused(app):
    client, *_ = app
    task = add(client)
    assert client.put(f"/api/tasks/{task['id']}/repeat",
                      json={"freq": "hourly"}).status_code == 422


def test_a_repeat_on_a_missing_task_is_a_404(app):
    client, *_ = app
    assert client.put("/api/tasks/nope/repeat",
                      json={"freq": "daily"}).status_code == 404


def test_the_calendar_says_which_blocks_come_back(app):
    client, *_ = app
    task = add(client)
    repeat(client, task["id"], freq="weekly", weekdays=[2])
    add(client, title="one-off")
    events = {e["title"]: e for e in client.get("/api/calendar").json()["events"]}
    assert events["take the bins out"]["recurrence"]["summary"] == "every week on Tue"
    assert events["one-off"]["recurrence"] is None


def test_the_series_endpoint_lists_every_rhythm(app):
    client, *_ = app
    task = add(client)
    repeat(client, task["id"], freq="monthly", month_day=1)
    body = client.get("/api/recurring").json()
    assert [s["summary"] for s in body["series"]] == ["every month on the 1st"]


def test_a_copy_lands_in_the_project_its_series_belongs_to(app):
    client, main, db, recurring = app
    other = client.post("/api/projects", json={"name": "House"}).json()
    house_id = other["active_project_id"]
    task = add(client, title="hoover")
    assert task["project_id"] == house_id
    repeat(client, task["id"], freq="daily", lead_days=0)
    client.post("/api/projects/%s/activate" % other["projects"][0]["id"])
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    advance(db, recurring)
    fresh = [t for t in db.list_tasks() if t["status"] == "todo"]
    assert len(fresh) == 1 and fresh[0]["project_id"] == house_id


def test_finishing_a_copy_still_pays_its_xp(app):
    client, main, db, _ = app
    task = add(client, impact=8, effort=2)
    repeat(client, task["id"], freq="daily")
    state = client.post(f"/api/tasks/{task['id']}/complete", json={}).json()
    assert state["xp"]["gained"] > 0


# ---------------------------------------------------------------------------
# the lead, counted in days
# ---------------------------------------------------------------------------

def test_a_lead_is_counted_in_days_not_in_hours():
    """"One day of lead" is a day, the way anyone saying it means a day.

    Measured as 24 hours instead, a chore due at six tomorrow evening is still
    out of reach at ten this morning — so finishing today's copy leaves an
    empty list and a rhythm that looks like it has stopped working.
    """
    rule = logic.normalize_rule({"freq": "daily", "lead_days": 1})
    now = local(2026, 3, 4, 10, 0)                      # Wednesday morning
    horizon = recurrence.lead_horizon(rule, now, NY)
    assert horizon == local(2026, 3, 6, 0, 0)           # the end of Thursday
    assert local(2026, 3, 5, 18, 0) < horizon           # tomorrow evening: yes
    assert local(2026, 3, 6, 18, 0) > horizon           # the day after: not yet
    none = logic.normalize_rule({"freq": "daily", "lead_days": 0})
    assert recurrence.lead_horizon(none, now, NY) == local(2026, 3, 5, 0, 0)


def test_finishing_todays_copy_puts_tomorrows_on_the_list(app):
    """The whole promise of a daily rhythm: tick it off, and the next is there."""
    client, main, db, _ = app
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                              microsecond=0)
    task = add(client, deadline=noon.isoformat())
    repeat(client, task["id"], freq="daily", lead_days=1)
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    open_now = [t for t in db.list_tasks() if t["status"] == "todo"]
    assert len(open_now) == 1
    assert open_now[0]["id"] != task["id"]
    assert logic.parse_dt(open_now[0]["deadline"]) == noon + timedelta(days=1)


def test_a_lead_of_none_still_waits_for_the_day_itself(app):
    """The other end of it: `lead_days=0` means the day it is due, and no
    amount of the evening before counts as that day."""
    client, main, db, recurring = app
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                              microsecond=0)
    task = add(client, deadline=noon.isoformat())
    repeat(client, task["id"], freq="daily", lead_days=0)
    client.post(f"/api/tasks/{task['id']}/complete", json={})
    assert len(db.list_tasks()) == 1
    due = noon + timedelta(days=1)
    recurring.sweep(due - timedelta(days=1, minutes=1))   # the day before: no
    assert len(db.list_tasks()) == 1
    recurring.sweep(due - timedelta(minutes=1))           # its own day: yes
    assert len(db.list_tasks()) == 2


# ---------------------------------------------------------------------------
# the forecast: the rhythm the list cannot show you
# ---------------------------------------------------------------------------

def test_the_forecast_is_the_beats_no_copy_has_been_made_for(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily", time="09:00")
    now = datetime.now(timezone.utc)
    ahead = recurring.forecast(days=10)
    assert len(ahead) >= 8
    # Every one of them is ahead of us, in order, and none of them is the
    # copy already on the list — that one is a real task with a real deadline
    # and counting it twice is exactly what this must not do.
    assert all(occ["at"] > now for occ in ahead)
    assert ahead == sorted(ahead, key=lambda o: o["at"])
    on_list = logic.parse_dt(db.list_tasks()[0]["deadline"])
    assert all(occ["at"] != on_list for occ in ahead)


def test_the_forecast_stops_where_the_rule_does(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily", count=3)
    # One copy is on the list already, so two beats are still owed.
    assert len(recurring.forecast(days=30)) == 2


def test_a_stopped_rhythm_forecasts_nothing(app):
    client, main, db, recurring = app
    task = add(client)
    repeat(client, task["id"], freq="daily")
    assert recurring.forecast(days=10)
    client.delete(f"/api/tasks/{task['id']}/repeat")
    assert recurring.forecast(days=10) == []


def test_the_forecast_carries_the_length_of_the_work(app):
    """A rhythm's future is only useful to a calendar if it has a size."""
    client, main, db, recurring = app
    task = add(client, estimated_time=60)
    repeat(client, task["id"], freq="daily")
    occ = recurring.forecast(days=3)[0]
    assert occ["minutes"] >= 60          # its estimate, plus the time tax
    assert occ["template"]["title"] == "take the bins out"


def test_a_rhythm_with_steps_is_worth_its_steps_and_not_twice(app):
    """A container is worth exactly the work inside it — counting both it and
    its steps would book every repeating plan twice over."""
    client, main, db, recurring = app
    parent = add(client, title="weekly review", estimated_time=15)
    for title in ("read the notes", "write the plan"):
        client.post("/api/tasks", json={"title": title, "annotate": False,
                                        "parent_id": parent["id"],
                                        "estimated_time": 30})
    repeat(client, parent["id"], freq="weekly", weekdays=[1])
    occ = recurring.forecast(days=21)[0]
    assert len(occ["template"]["subtasks"]) == 2
    assert occ["minutes"] == 78          # two half-hours, +30% time tax, once


def test_the_calendar_draws_the_copies_that_do_not_exist_yet(app):
    """The complaint this answers: a week of eight-hour days looked empty,
    because only one copy of a repeating job is ever on the list."""
    client, *_ = app
    task = add(client, estimated_time=480)
    repeat(client, task["id"], freq="daily", time="17:00")
    events = client.get("/api/calendar").json()["events"]
    real = [e for e in events if not e["projected"]]
    ahead = [e for e in events if e["projected"]]
    assert len(real) == 1
    assert len(ahead) > 20
    first = ahead[0]
    assert first["title"] == "take the bins out"
    assert first["length_min"] >= 480
    assert first["status"] == "planned"
    assert first["deadline_source"] == "recurring"
    assert first["recurrence"]["summary"] == "every day · at 17:00"
    # Clicking one has to reach something real: the copy on the list.
    assert first["source_task_id"] == task["id"]
    # And they are the *same* job, so no forecast lands on the day the real
    # copy already occupies.
    assert real[0]["deadline"] not in {e["deadline"] for e in ahead}


def test_new_work_is_scheduled_around_the_days_a_rhythm_owns(app):
    """The other half of the complaint: the scheduler booked work into days
    that were already going to be eight hours of the same job."""
    client, *_ = app
    work = add(client, title="work", estimated_time=480)
    repeat(client, work["id"], freq="weekly", weekdays=[1, 2, 3, 4, 5],
           time="17:00")
    add(client, title="fix the sink", estimated_time=60)
    events = client.get("/api/calendar").json()["events"]
    sink = next(e for e in events if e["title"] == "fix the sink")
    booked = {e["deadline"][:10] for e in events if e["title"] == "work"}
    assert sink["deadline"][:10] not in booked


def test_the_forecast_does_not_move_a_deadline_you_set(app):
    """Booking the future must not shove a fixed point off its day."""
    client, *_ = app
    task = add(client, estimated_time=480)
    repeat(client, task["id"], freq="daily", time="17:00")
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
        microsecond=0).isoformat()
    fixed = add(client, title="dentist", deadline=when, estimated_time=60)
    events = {e["title"]: e for e in client.get("/api/calendar").json()["events"]}
    assert logic.parse_dt(events["dentist"]["deadline"]) == logic.parse_dt(when)
    assert events["dentist"]["deadline_source"] == "user"
    assert fixed["id"]
