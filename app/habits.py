"""Routines: the things you do *again*, rather than the things you finish.

A task is done once and leaves your list. A routine is never done — the only
question it ever asks is "did you do it today", and the only answer it keeps
is a tick against a date. That is a different shape from everything else in
this app, which is why it is a different table and not a repeating task: a
repeating task that you skip leaves a missed occurrence behind and eats a slot
in the planner's day, and neither of those is what a missed run is. You just
didn't run. Tomorrow is a new day and the calendar remembers the gap.

So there is no scheduling here, no estimate, no score, no XP. There is a
frequency, which decides which days *count*; a tick per day; and the two
numbers that come out of those — the streak you are on and the longest one you
have managed. The heatmap is those same ticks drawn as a year of squares, in
the manner of GitHub's contribution graph, which is the only progress display
that has ever made a habit feel like something you are already in the middle
of rather than something you are failing at.

Everything in here is pure: days in, numbers out. The database holds check-in
rows, `main` decides which local day "today" is, and this module decides what
those rows mean.
"""

from __future__ import annotations

from datetime import date, timedelta

# The four parts of a life this app is willing to have an opinion about. Fixed
# on purpose: a routine that does not fit one of them is almost always a task,
# and an editable taxonomy is a thing to maintain rather than a thing to use.
CATEGORIES = (
    {"id": "life", "label": "Life", "emoji": "🏠",
     "note": "Chores, admin, the things that keep the lights on"},
    {"id": "health", "label": "Health", "emoji": "💊",
     "note": "Meds, sleep, food, water, appointments"},
    {"id": "exercise", "label": "Exercise", "emoji": "🏃",
     "note": "Moving your body, however that looks today"},
    {"id": "mentality", "label": "Mentality", "emoji": "🧠",
     "note": "Headspace — journalling, meditation, therapy homework"},
)

CATEGORY_IDS = tuple(c["id"] for c in CATEGORIES)
DEFAULT_CATEGORY = "life"

# Three frequency shapes, which between them cover what people actually keep:
#   daily     — every day, no exceptions to track
#   weekdays  — a fixed set of days (0=Sunday, the numbering the repeat rules
#               and the calendar already speak)
#   weekly    — a target of N days a week, any days. "Run three times a week"
#               is a real habit and pinning it to named days makes it a lie.
# Anything richer (every 3 days, N times a month) was deliberately left out:
# its streak has no honest definition when you tick early or late, and a
# streak you cannot trust is worse than no streak.
RULE_TYPES = ("daily", "weekdays", "weekly")

WEEKDAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
WEEKDAY_LONG = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday")

# A year of squares, plus enough slack to fill the first and last columns.
HEATMAP_DAYS = 371          # 53 weeks
RATE_WINDOW = 30            # days the "how it's going" percentage looks back on

MAX_WEEKLY_TIMES = 7


def js_weekday(day: date) -> int:
    """0=Sunday..6=Saturday — the convention the whole app speaks."""
    return (day.weekday() + 1) % 7


# ---------- rules ----------

def normalize_rule(raw: dict | None) -> dict:
    """A stored, complete frequency rule from whatever the page sent.

    Raises ValueError with something a person could act on: this is reached
    straight from the API, and "invalid rule" tells nobody anything.
    """
    raw = raw or {}
    kind = str(raw.get("type") or "daily").strip()
    if kind not in RULE_TYPES:
        raise ValueError(f"Unknown frequency: {kind}")
    if kind == "daily":
        return {"type": "daily"}
    if kind == "weekdays":
        days = raw.get("days") or []
        try:
            days = sorted({int(d) for d in days})
        except (TypeError, ValueError):
            raise ValueError("Which days is a list of numbers, 0 (Sunday) to 6")
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Pick at least one day of the week")
        # Every day of the week named is just "daily" wearing a longer rule —
        # and a daily habit's streak counts days rather than a subset of them,
        # so they had better not be two different things.
        if len(days) == 7:
            return {"type": "daily"}
        return {"type": "weekdays", "days": days}
    times = raw.get("times")
    try:
        times = int(times)
    except (TypeError, ValueError):
        raise ValueError("How many times a week is a number")
    if times < 1 or times > MAX_WEEKLY_TIMES:
        raise ValueError(f"A week holds 1 to {MAX_WEEKLY_TIMES} of these")
    if times == MAX_WEEKLY_TIMES:
        return {"type": "daily"}
    return {"type": "weekly", "times": times}


def rule_label(rule: dict) -> str:
    """The rule in a handful of words, for the row and the dialog's preview."""
    kind = rule.get("type")
    if kind == "weekdays":
        days = rule.get("days") or []
        if days == [1, 2, 3, 4, 5]:
            return "Every weekday"
        if days == [0, 6]:
            return "Weekends"
        if len(days) == 1:
            return f"Every {WEEKDAY_LONG[days[0]]}"
        return ", ".join(WEEKDAY_NAMES[d] for d in days)
    if kind == "weekly":
        return f"{rule.get('times', 1)}× a week"
    return "Every day"


def is_due(rule: dict, day: date) -> bool:
    """Does this routine want doing on `day`?

    A weekly target has no due days — every day is a chance to hit it — so it
    answers yes to all of them, and its streak is counted in weeks instead.
    """
    kind = rule.get("type")
    if kind == "weekdays":
        return js_weekday(day) in set(rule.get("days") or ())
    return True


def counts_by_week(rule: dict) -> bool:
    """Is this routine's streak measured in weeks rather than days?"""
    return rule.get("type") == "weekly"


def week_start(day: date, first_day: int = 0) -> date:
    """The start of `day`'s week, honouring the app's week-start setting."""
    first_day = 0 if first_day not in (0, 1) else first_day
    return day - timedelta(days=(js_weekday(day) - first_day) % 7)


# ---------- what the ticks add up to ----------

def _day_streaks(rule: dict, ticked: set[date], today: date,
                 first: date) -> tuple[int, int]:
    """(current, longest) for a rule whose unit is the day.

    Walking back from today, a due day that was ticked extends the run and a
    due day that wasn't ends it — except today itself, which is still in
    progress. A day the rule doesn't name is skipped rather than counted: not
    running on a Sunday you never said you'd run on is not a broken streak.
    """
    current = 0
    day = today
    while day >= first:
        if is_due(rule, day):
            if day in ticked:
                current += 1
            elif day != today:
                break
        day -= timedelta(days=1)

    longest = run = 0
    day = first
    while day <= today:
        if is_due(rule, day):
            if day in ticked:
                run += 1
                longest = max(longest, run)
            elif day != today:
                # Today unticked leaves the run standing — it is not over yet,
                # and the current streak above is the number that says so.
                run = 0
        day += timedelta(days=1)
    return current, max(longest, current)


def _week_streaks(rule: dict, ticked: set[date], today: date, first: date,
                  first_day: int) -> tuple[int, int]:
    """(current, longest) in weeks, for an N-times-a-week target.

    A week counts once it holds `times` ticks. This week is allowed to be
    short without ending anything — there are days left in it — which is the
    whole reason a weekly target is easier to keep than a daily one.
    """
    times = int(rule.get("times") or 1)
    per_week: dict[date, int] = {}
    for day in ticked:
        key = week_start(day, first_day)
        per_week[key] = per_week.get(key, 0) + 1

    this_week = week_start(today, first_day)
    week = this_week
    current = 0
    while week >= week_start(first, first_day):
        met = per_week.get(week, 0) >= times
        if met:
            current += 1
        elif week != this_week:
            break
        week -= timedelta(days=7)

    longest = run = 0
    week = week_start(first, first_day)
    while week <= this_week:
        if per_week.get(week, 0) >= times:
            run += 1
            longest = max(longest, run)
        elif week != this_week:
            run = 0
        week += timedelta(days=7)
    return current, max(longest, current)


def stats(rule: dict, days: list[str] | set[str], today: date,
          first_day: int = 0) -> dict:
    """Everything a routine's row says about itself.

    `days` is every check-in this routine has, as ISO dates. Counted whole
    rather than over a window: a streak that quietly reset because it ran off
    the end of the heatmap would be a lie told by an implementation detail.
    """
    ticked = set()
    for value in days:
        try:
            ticked.add(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    # Ticks dated in the future are not evidence of anything yet.
    ticked = {d for d in ticked if d <= today}

    done_today = today in ticked
    due_today = is_due(rule, today)
    weekly = counts_by_week(rule)

    if not ticked:
        current = longest = 0
    else:
        first = min(ticked)
        if weekly:
            current, longest = _week_streaks(rule, ticked, today, first, first_day)
        else:
            current, longest = _day_streaks(rule, ticked, today, first)

    # How it has actually been going lately, as a percentage of the chances it
    # had. The one number that keeps a long streak honest — a 40-day streak on
    # a three-day-a-week rule is 17 runs, and the rate says so.
    window_start = today - timedelta(days=RATE_WINDOW - 1)
    if weekly:
        # Chances = the target across every whole week in the window; the
        # current week is counted by what is left of it, not by its target.
        chances = 0
        week = week_start(window_start, first_day)
        while week <= today:
            chances += int(rule.get("times") or 1)
            week += timedelta(days=7)
    else:
        chances = sum(
            1 for i in range(RATE_WINDOW)
            if is_due(rule, window_start + timedelta(days=i)))
    hits = sum(1 for d in ticked if window_start <= d <= today)
    rate = min(1.0, hits / chances) if chances else 0.0

    this_week = 0
    if weekly:
        start = week_start(today, first_day)
        this_week = sum(1 for d in ticked if start <= d <= today)

    return {
        "current": current,
        "longest": longest,
        "unit": "week" if weekly else "day",
        "total": len(ticked),
        "rate": round(rate, 3),
        "rate_days": RATE_WINDOW,
        "due_today": due_today,
        "done_today": done_today,
        # Only meaningful for a weekly target, and the page only reads it there.
        "this_week": this_week,
        "target": int(rule.get("times") or 0) if weekly else 0,
    }


def heatmap_window(today: date, days: int = HEATMAP_DAYS,
                   first_day: int = 0) -> tuple[date, date]:
    """The span the calendar draws: whole weeks, ending with today's.

    Starting mid-week would leave a ragged first column that reads as missed
    days rather than as days before you started, so the window is snapped back
    to a week boundary.
    """
    end = today
    start = week_start(end - timedelta(days=max(1, days) - 1), first_day)
    return start, end
