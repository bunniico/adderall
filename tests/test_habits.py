"""Routines: what the ticks mean, and the API that collects them.

The streak is the whole feature. Everything else on the Habits tab is a
drawing of it, so the cases below are the ones where "did you keep it up" has
an answer that is not obvious: a day the rule never asked for, a today that
has not happened yet, a weekly target that is only half met, and a rule that
changed after the days were already ticked.
"""

import importlib
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import habits


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "habits.db"))
    from app import db
    importlib.reload(db)
    from app import main
    importlib.reload(main)
    return TestClient(main.app)


def iso(day):
    return day.isoformat()


def days_back(today, *offsets):
    return [iso(today - timedelta(days=n)) for n in offsets]


# ---------- rules ----------

def test_every_weekday_named_is_just_daily():
    """Seven days picked is "daily" wearing a longer rule, and it had better
    not be a second kind of thing: a daily streak counts days, and a weekdays
    streak counts a subset of them."""
    assert habits.normalize_rule({"type": "weekdays", "days": [0, 1, 2, 3, 4, 5, 6]}) \
        == {"type": "daily"}
    assert habits.normalize_rule({"type": "weekly", "times": 7}) == {"type": "daily"}


def test_rules_that_mean_nothing_are_refused():
    with pytest.raises(ValueError):
        habits.normalize_rule({"type": "weekdays", "days": []})
    with pytest.raises(ValueError):
        habits.normalize_rule({"type": "weekdays", "days": [9]})
    with pytest.raises(ValueError):
        habits.normalize_rule({"type": "weekly", "times": 0})
    with pytest.raises(ValueError):
        habits.normalize_rule({"type": "fortnightly"})


def test_rule_labels_read_like_english():
    assert habits.rule_label({"type": "daily"}) == "Every day"
    assert habits.rule_label({"type": "weekdays", "days": [1, 2, 3, 4, 5]}) \
        == "Every weekday"
    assert habits.rule_label({"type": "weekdays", "days": [0, 6]}) == "Weekends"
    assert habits.rule_label({"type": "weekdays", "days": [3]}) == "Every Wednesday"
    assert habits.rule_label({"type": "weekly", "times": 3}) == "3× a week"


def test_due_days_follow_the_rule():
    monday = date(2026, 9, 21)
    assert monday.weekday() == 0
    weekdays = {"type": "weekdays", "days": [1, 3, 5]}   # Mon, Wed, Fri
    assert habits.is_due(weekdays, monday)
    assert not habits.is_due(weekdays, monday + timedelta(days=1))
    # A weekly target owes no particular day, so every day is a chance at it.
    assert habits.is_due({"type": "weekly", "times": 3}, monday + timedelta(days=1))


# ---------- streaks ----------

DAILY = {"type": "daily"}


def test_daily_streak_counts_back_from_today():
    today = date(2026, 9, 21)
    stats = habits.stats(DAILY, days_back(today, 0, 1, 2, 3), today)
    assert stats["current"] == 4
    assert stats["longest"] == 4
    assert stats["unit"] == "day"
    assert stats["done_today"] and stats["due_today"]


def test_today_not_ticked_yet_is_not_a_broken_streak():
    """The day is not over. A streak display that resets at midnight and
    stays reset until you tick is a display that tells you you have failed
    every morning."""
    today = date(2026, 9, 21)
    stats = habits.stats(DAILY, days_back(today, 1, 2, 3), today)
    assert stats["current"] == 3
    assert stats["done_today"] is False


def test_a_missed_day_ends_the_run_but_not_the_record():
    today = date(2026, 9, 21)
    # Five in a row a while back, then a gap, then two.
    ticked = days_back(today, 1, 2, 10, 11, 12, 13, 14)
    stats = habits.stats(DAILY, ticked, today)
    assert stats["current"] == 2
    assert stats["longest"] == 5
    assert stats["total"] == 7


def test_a_day_the_rule_never_asked_for_cannot_break_a_streak():
    """Not running on a Sunday you never said you'd run on is not a failure,
    and a streak that counts it as one is a streak nobody trusts."""
    # Mon/Wed/Fri, ticked on three of them across a fortnight.
    friday = date(2026, 9, 18)
    rule = {"type": "weekdays", "days": [1, 3, 5]}
    ticked = [iso(friday), iso(friday - timedelta(days=2)),      # Wed
              iso(friday - timedelta(days=4))]                   # Mon
    stats = habits.stats(rule, ticked, friday)
    assert stats["current"] == 3
    # And the weekend in the middle of it is neither owed nor counted.
    assert not habits.is_due(rule, friday + timedelta(days=1))


def test_weekly_target_streaks_are_counted_in_weeks():
    """"Three times a week" is met by any three days, so its streak is a run
    of weeks rather than a run of days."""
    # A Monday, with week_start=0 (Sunday) — so this week began yesterday.
    today = date(2026, 9, 21)
    rule = {"type": "weekly", "times": 3}
    # Three days in each of the two previous weeks, one so far in this one.
    ticked = days_back(today, 0, 2, 3, 5, 9, 10, 12)
    stats = habits.stats(rule, ticked, today)
    assert stats["unit"] == "week"
    # Two full weeks behind, and this one is still in progress rather than
    # already failed — which is the whole reason a weekly target is keepable.
    assert stats["current"] == 2
    assert stats["this_week"] == 1
    assert stats["target"] == 3


def test_a_weekly_target_missed_outright_ends_the_run():
    today = date(2026, 9, 21)
    rule = {"type": "weekly", "times": 3}
    # Last week got one day; the week before got three.
    ticked = days_back(today, 2, 9, 10, 12)
    assert habits.stats(rule, ticked, today)["current"] == 0


def test_the_rate_keeps_a_long_streak_honest():
    """A 40-day streak on a three-days-a-week rule is seventeen runs. The
    percentage is the number that says so."""
    today = date(2026, 9, 21)
    every_day = habits.stats(DAILY, days_back(today, *range(30)), today)
    assert every_day["rate"] == 1.0
    half = habits.stats(DAILY, days_back(today, *range(0, 30, 2)), today)
    assert 0.4 < half["rate"] < 0.6


def test_ticks_dated_in_the_future_count_for_nothing_yet():
    today = date(2026, 9, 21)
    stats = habits.stats(DAILY, [iso(today + timedelta(days=1))], today)
    assert stats["total"] == 0
    assert stats["current"] == 0


def test_the_heatmap_window_starts_on_a_week_boundary():
    """A ragged first column reads as missed days rather than as days before
    you started."""
    today = date(2026, 9, 21)
    start, end = habits.heatmap_window(today, first_day=0)
    assert end == today
    assert habits.js_weekday(start) == 0
    assert (end - start).days >= habits.HEATMAP_DAYS - 7


# ---------- the API ----------

def make(client, name="Take meds", category="health", rule=None):
    body = {"name": name, "category": category,
            "rule": rule or {"type": "daily"}}
    res = client.post("/api/habits", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def test_a_routine_is_created_counted_and_listed(client):
    payload = make(client)
    assert len(payload["habits"]) == 1
    habit = payload["habits"][0]
    assert habit["name"] == "Take meds"
    assert habit["rule_label"] == "Every day"
    assert habit["stats"]["current"] == 0
    assert habit["checkins"] == []
    # The four parts of a life are named by the server, in one place.
    assert [c["id"] for c in payload["categories"]] == list(habits.CATEGORY_IDS)
    assert payload["today_due"] == 1 and payload["today_done"] == 0
    assert client.get("/api/habits").json()["habits"][0]["id"] == habit["id"]


def test_ticking_today_fills_in_a_square_and_starts_a_streak(client):
    habit = make(client)["habits"][0]
    payload = client.post(f"/api/habits/{habit['id']}/check", json={"done": True}).json()
    row = payload["habits"][0]
    assert row["stats"]["current"] == 1
    assert row["stats"]["done_today"] is True
    assert payload["today"] in row["checkins"]
    assert payload["today_done"] == 1
    assert payload["today_clear"] is True


def test_a_square_filled_in_by_mistake_can_be_cleared(client):
    """A calendar you cannot correct is a calendar you stop trusting."""
    habit = make(client)["habits"][0]
    client.post(f"/api/habits/{habit['id']}/check", json={"done": True})
    payload = client.post(f"/api/habits/{habit['id']}/check",
                          json={"done": False}).json()
    assert payload["habits"][0]["stats"]["current"] == 0
    assert payload["habits"][0]["checkins"] == []


def test_ticking_the_same_day_twice_is_one_tick(client):
    habit = make(client)["habits"][0]
    client.post(f"/api/habits/{habit['id']}/check", json={"done": True})
    payload = client.post(f"/api/habits/{habit['id']}/check", json={"done": True}).json()
    assert payload["habits"][0]["stats"]["total"] == 1


def test_a_day_you_forgot_to_tick_can_be_filled_in_later(client):
    habit = make(client)["habits"][0]
    today = date.fromisoformat(client.get("/api/habits").json()["today"])
    for back in (0, 1, 2):
        client.post(f"/api/habits/{habit['id']}/check",
                    json={"day": iso(today - timedelta(days=back)), "done": True})
    assert client.get("/api/habits").json()["habits"][0]["stats"]["current"] == 3


def test_a_tick_in_the_future_is_a_typo(client):
    habit = make(client)["habits"][0]
    ahead = iso(date.today() + timedelta(days=30))
    res = client.post(f"/api/habits/{habit['id']}/check",
                      json={"day": ahead, "done": True})
    assert res.status_code == 400
    assert client.get("/api/habits").json()["habits"][0]["stats"]["total"] == 0


def test_nonsense_is_refused_with_something_you_could_act_on(client):
    assert client.post("/api/habits", json={"name": "x", "category": "vibes"}) \
        .status_code == 400
    bad = client.post("/api/habits",
                      json={"name": "x", "rule": {"type": "weekdays", "days": []}})
    assert bad.status_code == 400
    assert "day of the week" in bad.json()["detail"]
    # Three spaces is long enough for the field and still not a name.
    assert client.post("/api/habits", json={"name": "   "}).status_code == 400
    assert client.post("/api/habits", json={"name": ""}).status_code == 422
    assert client.patch("/api/habits/nope", json={"name": "x"}).status_code == 404
    assert client.post("/api/habits/nope/check", json={"done": True}).status_code == 404


def test_changing_the_rule_leaves_the_days_you_did_alone(client):
    """You did those days. What the routine asks for now does not change
    that — though it can change what they add up to."""
    habit = make(client, rule={"type": "daily"})["habits"][0]
    today = date.fromisoformat(client.get("/api/habits").json()["today"])
    for back in range(4):
        client.post(f"/api/habits/{habit['id']}/check",
                    json={"day": iso(today - timedelta(days=back)), "done": True})
    payload = client.patch(f"/api/habits/{habit['id']}",
                           json={"rule": {"type": "weekly", "times": 2}}).json()
    row = payload["habits"][0]
    assert row["stats"]["total"] == 4
    assert row["stats"]["unit"] == "week"
    assert row["rule_label"] == "2× a week"


def test_deleting_a_routine_takes_its_ticks_with_it(client):
    habit = make(client)["habits"][0]
    client.post(f"/api/habits/{habit['id']}/check", json={"done": True})
    payload = client.delete(f"/api/habits/{habit['id']}").json()
    assert payload["habits"] == []
    assert payload["today_due"] == 0
    assert payload["today_clear"] is False
    from app import db
    assert db.habit_checkins() == {}


def test_a_rest_day_is_not_an_open_routine(client):
    """Today's count is over what is actually due. A Sunday you never claimed
    is not a routine you are behind on."""
    payload = client.get("/api/habits").json()
    today = date.fromisoformat(payload["today"])
    # A rule that names every day except today.
    others = [d for d in range(7) if d != habits.js_weekday(today)]
    make(client, name="Gym", category="exercise",
         rule={"type": "weekdays", "days": others})
    payload = client.get("/api/habits").json()
    assert payload["today_due"] == 0
    assert payload["habits"][0]["stats"]["due_today"] is False


# ---------- the tab ----------

def test_habits_is_a_tab_you_go_to_not_a_list_you_switch_to(client):
    """Routines live in no project, so opening them must not change which
    list adding, braindumping and focusing act on."""
    before = client.get("/api/state").json()
    project_id = before["active_project_id"]

    opened = client.post("/api/projects/habits/activate").json()
    assert opened["habits"] is True
    assert opened["overview"] is False
    assert opened["active_project_id"] == project_id

    # The two places you can go are not both somewhere you are.
    overview = client.post("/api/projects/overview/activate").json()
    assert overview["overview"] is True and overview["habits"] is False

    back = client.post("/api/projects/habits/activate").json()
    assert back["habits"] is True and back["overview"] is False

    # ...and a real tab puts both of them away.
    listed = client.post(f"/api/projects/{project_id}/activate").json()
    assert listed["habits"] is False and listed["overview"] is False
    assert listed["active_project_id"] == project_id


def test_the_tab_you_were_on_survives_a_reload(client):
    client.post("/api/projects/habits/activate")
    assert client.get("/api/state").json()["habits"] is True


def test_a_new_project_puts_the_habits_tab_away(client):
    """Adding a list is always a list you want to start filling in."""
    client.post("/api/projects/habits/activate")
    made = client.post("/api/projects", json={"name": "Side quest"}).json()
    assert made["habits"] is False
    assert made["active_project_id"] == made["projects"][-1]["id"]


def test_routines_stay_out_of_the_task_list_entirely(client):
    """The whole reason this is not a repeating task: a routine cannot be
    late, cannot be scheduled, and cannot eat an afternoon of your day."""
    make(client, name="Walk", category="exercise")
    state = client.get("/api/state").json()
    assert state["tasks"] == []
    assert state["alarm_tasks"] == []
    assert client.get("/api/calendar").json()["events"] == []
    assert client.get("/api/overview").json()["stats"]["open"] == 0
