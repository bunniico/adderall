"""The local regex half of title parsing: deadline/repeat words read off a
task's own title, and stripped out of it. No network, no AI — see
test_ai_client.py for the fallback that only runs when this finds nothing.
"""

from datetime import datetime, timezone

from app import title_parse

SETTINGS = {"timezone": "America/New_York", "day_start": 9, "day_capacity": 480,
            "adaptive_capacity": False}

# A fixed Tuesday, so "next/this <weekday>" and "tomorrow" have one right answer.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_a_plain_title_is_left_alone():
    got = title_parse.parse_title("Clean the garage", NOW, SETTINGS)
    assert got == {"clean_title": "Clean the garage", "deadline": None,
                   "repeat": None, "matched": False}


def test_a_bare_weekday_with_no_connector_is_not_a_deadline():
    got = title_parse.parse_title("Email Monday about the notes", NOW, SETTINGS)
    assert not got["matched"]
    assert got["clean_title"] == "Email Monday about the notes"


def test_tomorrow_sets_a_deadline_and_is_stripped():
    got = title_parse.parse_title("Pay rent tomorrow", NOW, SETTINGS)
    assert got["clean_title"] == "Pay rent"
    assert got["deadline"] is not None
    assert got["repeat"] is None


def test_next_weekday_skips_today_even_when_today_matches():
    # NOW is a Tuesday; "next tuesday" must mean a week out, not today.
    got = title_parse.parse_title("Standup next tuesday", NOW, SETTINGS)
    when = datetime.fromisoformat(got["deadline"])
    assert (when.astimezone(title_parse.logic.resolve_tz(SETTINGS["timezone"]))
            .date() - NOW.date()).days == 7


def test_this_weekday_can_mean_today():
    got = title_parse.parse_title("Call mom this tuesday", NOW, SETTINGS)
    when = datetime.fromisoformat(got["deadline"])
    local = when.astimezone(title_parse.logic.resolve_tz(SETTINGS["timezone"]))
    assert local.date() == NOW.astimezone(
        title_parse.logic.resolve_tz(SETTINGS["timezone"])).date()


def test_daily_sets_a_repeat_rule_and_no_deadline():
    got = title_parse.parse_title("Take out the trash every day", NOW, SETTINGS)
    assert got["clean_title"] == "Take out the trash"
    assert got["repeat"] == {"freq": "daily", "interval": 1}
    assert got["deadline"] is None


def test_every_other_day_sets_interval_two():
    got = title_parse.parse_title("Water the plants every other day", NOW, SETTINGS)
    assert got["repeat"]["interval"] == 2


def test_every_n_weeks_sets_interval():
    got = title_parse.parse_title("Backup files every 3 weeks", NOW, SETTINGS)
    assert got["repeat"] == {"freq": "weekly", "interval": 3}


def test_named_weekdays_are_collected_and_sorted():
    got = title_parse.parse_title("Team sync every thursday and monday", NOW, SETTINGS)
    assert got["repeat"] == {"freq": "weekly", "interval": 1, "weekdays": [0, 3]}


def test_a_deadline_and_a_repeat_can_both_be_set():
    got = title_parse.parse_title("Pay rent tomorrow, every month", NOW, SETTINGS)
    assert got["deadline"] is not None
    assert got["repeat"] == {"freq": "monthly", "interval": 1}
    assert got["clean_title"] == "Pay rent"


def test_an_explicit_time_is_used_over_the_default():
    got = title_parse.parse_title("Call mom this sunday at 6pm", NOW, SETTINGS)
    when = datetime.fromisoformat(got["deadline"])
    local = when.astimezone(title_parse.logic.resolve_tz(SETTINGS["timezone"]))
    assert (local.hour, local.minute) == (18, 0)


def test_an_ambiguous_bare_hour_with_no_am_pm_is_not_read_as_a_time():
    got = title_parse.parse_title("Standup every day at 9", NOW, SETTINGS)
    assert got["repeat"] == {"freq": "daily", "interval": 1}
    # "at 9" stays in the title rather than being guessed at.
    assert "at 9" in got["clean_title"]


def test_a_repeat_time_carries_onto_the_rule():
    got = title_parse.parse_title("Standup every day at 9am", NOW, SETTINGS)
    assert got["repeat"] == {"freq": "daily", "interval": 1, "time": "09:00"}


def test_a_bare_frequency_word_naming_the_task_is_not_a_repeat():
    # "weekly" here names the task, it is not an instruction to repeat one —
    # only the explicit "every ..." phrasing may be read that way.
    got = title_parse.parse_title("Weekly review", NOW, SETTINGS)
    assert not got["matched"]
    assert got["clean_title"] == "Weekly review"


def test_in_n_days_sets_a_relative_deadline():
    got = title_parse.parse_title("Buy milk in 3 days", NOW, SETTINGS)
    when = datetime.fromisoformat(got["deadline"])
    local_date = when.astimezone(title_parse.logic.resolve_tz(SETTINGS["timezone"])).date()
    assert (local_date - NOW.astimezone(
        title_parse.logic.resolve_tz(SETTINGS["timezone"])).date()).days == 3


def test_a_title_that_is_only_a_trigger_phrase_keeps_something_to_show():
    got = title_parse.parse_title("tomorrow", NOW, SETTINGS)
    assert got["matched"]
    assert got["clean_title"] == "tomorrow"
