"""Deterministic scheduling, buffering, and prioritization.

Everything in this module is pure local computation: the time-tax buffer,
action-priority-matrix placement, urgency scoring, auto-deadlines with
backward scheduling, load-aware placement into days that have room, the
learned daily cap, and the "what next" ordering. No AI calls here — that
keeps recomputation instant, consistent, and testable.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

BUFFER_MIN = 0.25
BUFFER_MAX = 0.50
DEFAULT_ESTIMATE_MIN = 30  # used for urgency math when a task has no estimate

QUADRANT_RANK = {"quick_win": 0, "major_project": 1, "fill_in": 2, "thankless": 3, None: 2}

# Default deadline horizons (days) per quadrant for standalone auto-deadlines:
# quick wins surface soon, major projects get lead time, thankless sink.
HORIZON_DAYS = {"quick_win": 1, "fill_in": 3, "major_project": 7, "thankless": 14, None: 3}

ACTIVE_STATUSES = {"todo", "in_progress"}

# ---- how much a day is allowed to hold ----
# Eight hours is a preference, not a law of physics: it is what the calendar
# warns against overshooting and what auto-deadlines pack a day up to. It is
# also the number most likely to be wrong for any particular person, so it
# learns — see `capacity_plan`.
DEFAULT_CAPACITY = 480          # the classic eight hours, in minutes
CAPACITY_FLOOR = 60             # a learned cap never drops below an hour
CAPACITY_CEILING = 960          # ...nor climbs past sixteen
CAPACITY_MIN_DAYS = 5           # days of evidence before it says anything at all
CAPACITY_COMFORT = (0.30, 0.75) # hit rates that mean the goal is doing its job
CAPACITY_STEP = 0.5             # how far it moves toward the day you really have
CAPACITY_ROUND = 15             # learned caps land on a readable quarter hour

DEFAULT_DAY_START = 9           # local hour the working window opens
# How far ahead the planner will look for a day with room before it gives up
# and lets something double-book. Half a year is far past the point where the
# answer is "you have too much on", not "the app picked the wrong Tuesday".
PLACEMENT_SEARCH_DAYS = 180

# Priority score weights, over the four things the app knows about a task.
# Urgency carries the Eisenhower half (how close the deadline is relative to
# the work left) and impact the importance half; the other two are the cost of
# doing it, split because they are not the same cost. Ease is the inverse of
# effort — how hard the task is to face — and brevity the inverse of time cost,
# how long it will actually take. A form you dread for ten minutes is cheap on
# the clock and dear in effort; three hours of mindless data entry is the other
# way round, and a score built on effort alone calls them the same task.
# They sum to 1.0, so the score lands on a 0-100 scale you can read at a glance.
SCORE_WEIGHTS = {"urgency": 0.40, "impact": 0.30, "ease": 0.15, "brevity": 0.15}
NEUTRAL_SCORE = 5  # stand-in for an impact/effort the AI hasn't filled in yet

# Minutes at which time cost scores a middling 5 — an hour, which is also what
# an un-estimated task is treated as being worth. The curve below is a decay
# rather than a line on purpose: the gap between ten minutes and half an hour
# is the difference between doing it now and scheduling it, while the gap
# between six hours and eight is no difference at all — both are "not today".
TIME_COST_HALFLIFE = 60

# Minutes of lead time at which a start time scores a middling 5 — four hours,
# an afternoon. "Eat dinner" set at lunchtime scores about 5 and climbs to 8 an
# hour before, 10 when the hour arrives; "finish that game", set a month out,
# scores the 0.5 floor and stays out of the way until it is nearly time.
START_PRESSURE_HALFLIFE = 240

# ---- how the list is sorted ----
# "smart" is the app's own opinion (urgency, then quadrant); "manual" is the
# order you dragged things into. The rest are plain one-field sorts, there for
# the moments when you want to read the list a particular way — biggest first,
# soonest first, oldest first — rather than argue with the app about it.
SORT_FIELDS = ("smart", "manual", "score", "deadline", "subtasks", "created")

# Which way round a field means by default: nobody picks "deadline" wanting the
# furthest one first, or "score" wanting the least worthwhile task at the top.
DEFAULT_SORT_DIR = {
    "smart": "desc", "manual": "asc", "score": "desc",
    "deadline": "asc", "subtasks": "desc", "created": "desc",
}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def effective_buffer(settings: dict, ratios: list[float]) -> float:
    """The buffer actually applied: the configured base, raised (never lowered)
    by the user's own overshoot history when adaptive buffering is on."""
    base = min(BUFFER_MAX, max(BUFFER_MIN, float(settings.get("buffer", 0.30))))
    if settings.get("adaptive_buffer") and len(ratios) >= 3:
        learned = round(sum(ratios) / len(ratios) - 1.0, 4)
        return min(BUFFER_MAX, max(base, learned))
    return base


def buffered_estimate(estimated_time: int | None, buffer: float) -> int | None:
    if estimated_time is None:
        return None
    # Ceil, never round down: the whole point of the time tax is honesty
    # about tasks taking longer than they feel like they will.
    return max(1, math.ceil(estimated_time * (1.0 + buffer) - 1e-9))


def quadrant(impact: int | None, effort: int | None, threshold: int = 5) -> str | None:
    if impact is None or effort is None:
        return None
    high_impact = impact >= threshold
    high_effort = effort >= threshold
    if high_impact and not high_effort:
        return "quick_win"
    if high_impact and high_effort:
        return "major_project"
    if not high_impact and not high_effort:
        return "fill_in"
    return "thankless"


def urgency(deadline: datetime | None, buffered_min: int | None, now: datetime,
            start_at: datetime | None = None) -> float:
    """0.5 (no deadline / distant) → 10 (overdue, no slack, or time to start).

    Rises as remaining time shrinks relative to the buffered estimate: a task
    whose remaining window is barely bigger than the work itself has no slack.

    A task also becomes urgent simply because the hour it meant to begin at
    has arrived — dinner at seven is a seven o'clock problem however long you
    have to eat it — so the two readings are combined by taking whichever is
    higher. `max` and not a sum, deliberately: an auto-scheduled task is
    placed *at* its start time, so its deadline pressure and its start
    pressure are two views of the same fact and adding them would count it
    twice.
    """
    pressure = start_pressure(start_at, now)
    if deadline is None:
        return 0.5 if pressure is None else max(0.5, pressure)
    remaining_min = (deadline - now).total_seconds() / 60.0
    if remaining_min <= 0:
        return 10.0
    work = buffered_min if buffered_min and buffered_min > 0 else DEFAULT_ESTIMATE_MIN
    value = round(min(10.0, max(0.5, 10.0 * work / remaining_min)), 1)
    return value if pressure is None else max(value, pressure)


def start_pressure(start_at: datetime | None, now: datetime) -> float | None:
    """0.5 → 10: how loudly a task's own start time says "now" — or None.

    None, not neutral, when there is no start time: a task that never said
    when it wanted to begin is saying nothing, which has to read differently
    from one that said "not for a fortnight".

    The curve decays with the wait rather than falling in a straight line,
    because that is the shape of the question the feature exists to answer:
    something meant for the next hour or two is a today problem, something
    four hours out is a maybe, and a fortnight out is not a problem at all.
    A start time already gone by pegs the scale, exactly as a missed deadline
    does — the work is ready and you are not doing it.
    """
    if start_at is None:
        return None
    lead = (start_at - now).total_seconds() / 60.0
    if lead <= 0:
        return 10.0
    return round(max(0.5, 10.0 * START_PRESSURE_HALFLIFE / (START_PRESSURE_HALFLIFE + lead)), 1)


# ---------------------------------------------------------------------------
# Days: how long one holds, and where the next thing fits in it
# ---------------------------------------------------------------------------
# The server stores instants and has no timezone of its own, but a *day* is a
# local idea and so is "eight hours of it". Everything in this section works in
# local days and local minutes-past-midnight, and hands back UTC instants,
# which is the only thing the rest of the app stores.


def resolve_tz(name: str | None) -> tzinfo:
    """The timezone days are measured in.

    The page reports its own zone the first time it loads. Anything missing or
    unrecognisable falls back to UTC rather than failing a request over a
    scheduling nicety — a plan an hour out of place beats no plan at all.
    """
    name = (name or "").strip()
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def daily_totals(history: list[dict], tz: tzinfo = timezone.utc) -> dict[str, int]:
    """Minutes of work finished per local day.

    Only days you finished *something* on are counted. A weekend off is not
    evidence that you can manage twenty minutes a day, and letting it count as
    one would drag the cap down to nothing over a fortnight.
    """
    totals: dict[str, int] = {}
    for row in history:
        when = parse_dt(row.get("finished_at"))
        minutes = row.get("minutes")
        if when is None or not minutes:
            continue
        key = when.astimezone(tz).date().isoformat()
        totals[key] = totals.get(key, 0) + int(minutes)
    return totals


def capacity_plan(settings: dict, history: list[dict] | None = None,
                  tz: tzinfo | None = None) -> dict:
    """How much work one day is allowed to hold, and how that number was reached.

    The configured cap is the goal. Whether it is a *useful* goal shows in how
    often it is actually reached: one you clear on nine days in ten is set too
    low to mean anything, and one you have never once reached isn't a plan,
    it's a daily notification that you failed — which is the exact failure mode
    this app exists to avoid. So when the hit rate leaves the comfortable band,
    the cap moves halfway toward the day you actually have, and the calendar's
    warning moves with it.

    Returns the workings, not just the answer: a cap that silently disagrees
    with the one in Settings is worse than no cap at all, so the calendar can
    say out loud which number it is using and why.
    """
    base = int(settings.get("day_capacity") or DEFAULT_CAPACITY)
    base = max(CAPACITY_FLOOR, min(CAPACITY_CEILING, base))
    plan = {
        "minutes": base, "base": base, "typical": None, "hit_rate": None,
        "days": 0, "learned": False,
        "adaptive": bool(settings.get("adaptive_capacity", True)),
    }
    tz = tz or resolve_tz(settings.get("timezone"))
    totals = sorted(daily_totals(history or [], tz).values())
    plan["days"] = len(totals)
    if not plan["adaptive"] or len(totals) < CAPACITY_MIN_DAYS:
        return plan

    hit_rate = sum(1 for t in totals if t >= base) / len(totals)
    typical = _median(totals)
    plan["hit_rate"] = round(hit_rate, 2)
    plan["typical"] = int(round(typical))
    if CAPACITY_COMFORT[0] <= hit_rate <= CAPACITY_COMFORT[1]:
        return plan  # you reach it often enough for it to mean something

    # Halfway, not all the way: one unusually good (or unusually flattened)
    # fortnight shouldn't redefine what a day is.
    moved = base + (typical - base) * CAPACITY_STEP
    minutes = int(round(moved / CAPACITY_ROUND) * CAPACITY_ROUND)
    plan["minutes"] = max(CAPACITY_FLOOR, min(CAPACITY_CEILING, minutes))
    plan["learned"] = plan["minutes"] != base
    return plan


class DayPlanner:
    """A book of what each day already holds, and where the next thing fits.

    Auto-deadlines used to be a horizon and nothing else — created plus so
    many days — so fifteen things braindumped in one minute came back as
    fifteen blocks stacked on the same afternoon, at the same instant, on a
    day that could never have held them. That is a plan you bounce off rather
    than start.

    This keeps the days themselves. Every commitment already made (a deadline
    you set, a tree it has already placed) is booked against a local day, and
    a new one is handed the first slot that fits inside a working window that
    still has room under the cap. It only ever looks *forward* from the day
    the horizon asked for: pulling work earlier would invent urgency nobody
    asked for.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 day_start: int = DEFAULT_DAY_START,
                 tz: tzinfo = timezone.utc,
                 search_days: int = PLACEMENT_SEARCH_DAYS) -> None:
        self.capacity = max(30, min(24 * 60, int(capacity or DEFAULT_CAPACITY)))
        self.day_start = max(0, min(23, int(day_start))) * 60
        self.tz = tz
        self.search_days = max(1, int(search_days))
        self._days: dict[date, list[list[int]]] = {}
        self._placed: dict[str, datetime] = {}

    # ---- local time <-> instants ----

    def _local(self, when: datetime) -> datetime:
        return when.astimezone(self.tz)

    def local_day(self, when: datetime) -> date:
        """Which local day an instant falls on — the unit everything here
        books against, and the one the caller has to rank by."""
        return self._local(when).date()

    def _midnight(self, day: date) -> datetime:
        return datetime.combine(day, time(0, 0), tzinfo=self.tz)

    def _instant(self, day: date, minute: float) -> datetime:
        return (self._midnight(day) + timedelta(minutes=minute)).astimezone(timezone.utc)

    @property
    def window_end(self) -> int:
        """Last local minute of the working window — the cap, from day_start."""
        return min(24 * 60, self.day_start + self.capacity)

    # ---- the book ----

    def book(self, start: datetime, end: datetime) -> None:
        """Mark a span busy, split across every local day it touches."""
        start, end = self._local(start), self._local(end)
        if end <= start:
            end = start + timedelta(minutes=1)
        day, last = start.date(), end.date()
        while day <= last:
            base = self._midnight(day)
            first = max(0.0, (start - base).total_seconds() / 60.0)
            final = min(24 * 60.0, (end - base).total_seconds() / 60.0)
            if final > first:
                self._days.setdefault(day, []).append([int(first), math.ceil(final)])
            day += timedelta(days=1)

    def _merged(self, day: date) -> list[list[int]]:
        merged: list[list[int]] = []
        for start, end in sorted(self._days.get(day, [])):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return merged

    def load(self, day: date) -> int:
        """Minutes already committed on a local day, at whatever hour they sit."""
        return sum(end - start for start, end in self._merged(day))

    def _floor(self, day: date, not_before: datetime | None,
               base: int | None = None) -> int:
        """Earliest local minute a new block may start on this day.

        Only the day you are standing in is clamped: a slot at 9am is no use
        at four in the afternoon. Days that are wholly in the past are left
        alone, so work that is already overdue stays overdue instead of
        quietly rescheduling itself out of the red.

        `base` is the bottom of the search — the top of the working window by
        default, or the minute a start time asks for, which may well sit
        outside it.
        """
        floor = self.day_start if base is None else base
        if not_before is None:
            return floor
        local = self._local(not_before)
        if local.date() != day:
            return floor
        into_day = (local - self._midnight(day)).total_seconds() / 60.0
        return max(floor, int(math.ceil(into_day)))

    def _minute_of(self, day: date, when: datetime) -> int:
        """`when` as a local minute of `day` — how a start time is booked."""
        into_day = (self._local(when) - self._midnight(day)).total_seconds() / 60.0
        return int(math.ceil(into_day))

    def _gaps(self, day: date, floor: int,
              until: int | None = None) -> list[tuple[int, int]]:
        """Free stretches of the working window, in local minutes.

        `until` moves the far end of that window, which is what lets a task
        with a start time in the evening be placed at all: office hours are
        where the app puts work it chose the hour for itself, not a rule about
        when you are allowed to eat dinner.
        """
        end_of_window = self.window_end if until is None else until
        free: list[tuple[int, int]] = []
        cursor = max(0, floor)
        for start, end in self._merged(day):
            if end <= cursor or start >= end_of_window:
                continue
            start, end = max(start, cursor), min(end, end_of_window)
            if start > cursor:
                free.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < end_of_window:
            free.append((cursor, end_of_window))
        return free

    def _first_fit(self, day: date, length: int, not_before: datetime | None,
                   pin: datetime | None = None) -> tuple[date, int] | None:
        """The first day from `day` onward with both room under the cap and a
        gap long enough to hold the work in one piece.

        `pin` is the moment the task asked to begin at. On the day it falls
        on, the search starts there rather than at the top of the working
        window, the window stretches a day's worth past it so several pinned
        things can run back to back, and the cap does not get a veto: a start
        time is a commitment you made, like a deadline you set, and the cap
        governs the work the app schedules for you. On every later day the
        pin has nothing to say and the ordinary rules apply again.
        """
        for _ in range(self.search_days + 1):
            pinned = pin is not None and self._local(pin).date() == day
            base = self._minute_of(day, pin) if pinned else None
            floor = self._floor(day, not_before, base)
            until = (min(24 * 60, max(self.window_end, floor + self.capacity))
                     if pinned else None)
            if pinned or self.capacity - self.load(day) >= length:
                for start, end in self._gaps(day, floor, until):
                    if end - start >= length:
                        return day, start
            day += timedelta(days=1)
        return None

    def _emptiest(self, day: date) -> tuple[date, int]:
        """The last resort: the least-booked day from `day` on, and the first
        minute of it nothing has already claimed.

        This used to be "the first day nothing has claimed at all", which is
        the same answer on an empty book and a much worse one on a full book:
        a diary with eight hours of work in every day of it — a job you do
        every weekday, say — has no clear day in it, so everything that
        overflowed landed on one afternoon at one instant, which is the pile
        this planner exists to prevent. The emptiest day is the honest answer
        instead: the work goes where there is least of it already, after
        whatever is already booked there rather than on top of it, and each
        thing placed makes its day that much less empty for the next. Ties go
        to the earliest day, so a book with a clear day anywhere in it still
        gets that day, exactly as before.
        """
        best: tuple[int, date] | None = None
        for _ in range(self.search_days + 1):
            load = self.load(day)
            if best is None or load < best[0]:
                best = (load, day)
            if not load:
                break               # nothing is emptier than empty
            day += timedelta(days=1)
        chosen = best[1]
        booked = self._merged(chosen)
        return chosen, max(self.day_start, booked[-1][1] if booked else 0)

    # ---- what the scheduler calls ----

    def reserve(self, key: str, deadline: datetime, length: int) -> datetime:
        """Book something that already has a time: a deadline you set yourself.

        Fixed points go in before anything is placed around them, so the app
        schedules its own work in the space that is actually left.
        """
        if key in self._placed:
            return self._placed[key]
        length = max(1, int(length))
        self.book(deadline - timedelta(minutes=length), deadline)
        self._placed[key] = deadline
        return deadline

    def place(self, key: str, target: datetime, length: int,
              not_before: datetime | None = None,
              pin: datetime | None = None) -> datetime:
        """The deadline for `length` minutes of work wanted around `target`.

        Never earlier than the day the horizon asked for, and never onto a day
        that is already full while a later one has room. Repeat calls for the
        same task give the same answer, so a page reload doesn't reshuffle
        your week.

        `pin` is the task's own start time, when it has one: the hour it wants
        to begin at rather than a day for the app to choose within. See
        `_first_fit` for what that buys it.
        """
        if key in self._placed:
            return self._placed[key]
        length = max(1, int(length))
        day = self._local(target).date()
        slot = (self._first_fit(day, length, not_before, pin)
                if length <= self.capacity or pin is not None else None)
        if slot is None:
            # More than a whole day's worth of work in one piece (or nothing
            # free for half a year): give it the emptiest day there is and let
            # it run past the end of the window. A twelve-hour job is a
            # twelve-hour job, and pretending otherwise helps nobody.
            slot = self._emptiest(day)
        chosen, start = slot
        end = self._instant(chosen, start + length)
        self.book(end - timedelta(minutes=length), end)
        self._placed[key] = end
        return end


def day_planner(settings: dict, capacity: int | None = None) -> DayPlanner:
    """A planner set up the way the settings say a day works."""
    if capacity is None:
        capacity = capacity_plan(settings)["minutes"]
    return DayPlanner(capacity, settings.get("day_start", DEFAULT_DAY_START),
                      resolve_tz(settings.get("timezone")))


def _tree_minutes(task: dict, children: dict, lengths: dict) -> int:
    """Minutes of real work in a task's subtree.

    Leaves only: a container is worth exactly the steps it is made of, and
    counting both it and them would book every plan twice over — which is the
    difference between "you have four hours on Tuesday" and "you have eight".
    """
    kids = [c for c in children.get(task["id"], []) if c["status"] in ACTIVE_STATUSES]
    if kids:
        return sum(_tree_minutes(kid, children, lengths) for kid in kids)
    if task["status"] not in ACTIVE_STATUSES:
        return 0
    return max(1, int(lengths.get(task["id"]) or DEFAULT_ESTIMATE_MIN))


def reserve_fixed(planner: DayPlanner, tasks: list[dict], settings: dict,
                  ratios: list[float] | None = None) -> None:
    """Book every tree that already has a deadline you set.

    Run over every project *before* any auto-deadline is placed, so work in
    one tab is never dropped on top of a commitment in another: the calendar
    spans every project, and so does the day it is spending.

    A tree is booked as one span — the work its steps add up to, ending on its
    deadline — which is exactly the shape the calendar draws it in.
    """
    buf = effective_buffer(settings, ratios or [])
    children = _children_map(tasks)
    lengths = {t["id"]: buffered_estimate(t["estimated_time"], buf) for t in tasks}
    for task in children.get(None, []):
        deadline = parse_dt(task["deadline"])
        if deadline is None:
            continue
        minutes = _tree_minutes(task, children, lengths)
        if minutes:
            planner.reserve(task["id"], deadline, minutes)


def block_length(derived: dict) -> int:
    """How much time a task occupies, in minutes — the size of its calendar
    block and the length a nudge preserves.

    A container is worth the work it still holds, a leaf its own buffered
    estimate, and a task nobody has estimated yet gets the same default the
    urgency maths uses rather than a zero-height sliver.
    """
    if derived["has_subtasks"]:
        length = (derived["rollup_remaining"] or derived["rollup_estimate"]
                  or derived["buffered_estimate"])
    else:
        length = derived["buffered_estimate"]
    return max(1, int(length or DEFAULT_ESTIMATE_MIN))


def brevity(minutes: int | None) -> float:
    """0-10: how cheap a task is on the clock, from the time it will take.

    The inverse of time cost, on the same 0-10 scale as impact and effort so
    it can sit in the same weighted sum. It decays rather than falls in a
    straight line — ten minutes scores 8.6, an hour 5, a whole day about 1 —
    because that is how the choice actually feels: shaving twenty minutes off
    a half-hour job changes whether you do it now, shaving twenty off a
    six-hour one changes nothing.

    An un-estimated task sits at neutral, the same way an unrated one does.
    Guessing it is quick would reward never estimating anything.
    """
    if minutes is None or minutes <= 0:
        return float(NEUTRAL_SCORE)
    return 10.0 * TIME_COST_HALFLIFE / (TIME_COST_HALFLIFE + minutes)


def priority_score(impact: int | None, effort: int | None, urgency_value: float,
                   minutes: int | None = None) -> float:
    """0-100: one number for "what deserves the next hour".

    The week and month views put a whole day of tasks on screen at once, and
    a list in deadline order says nothing about which of them actually
    matters. This folds the four signals the app keeps — deadline pressure
    (urgency), importance (impact), and the two halves of what it costs you:
    effort, how hard it is to face, and time, how much of the day it eats.
    Both costs are counted as their inverse, so cheap wins float.

    Effort alone used to stand for the whole cost, which quietly made every
    two-minute email and every three-hour slog the same task as long as they
    felt equally unpleasant. `minutes` is the buffered estimate — the honest
    length, time tax included — and for a container the work still left inside
    it (see `block_length`).

    Unscored tasks sit at neutral rather than at zero: a task nobody has
    rated yet is unknown, not worthless.
    """
    imp = NEUTRAL_SCORE if impact is None else max(0, min(10, impact))
    eff = NEUTRAL_SCORE if effort is None else max(0, min(10, effort))
    raw = (SCORE_WEIGHTS["urgency"] * max(0.0, min(10.0, urgency_value))
           + SCORE_WEIGHTS["impact"] * imp
           + SCORE_WEIGHTS["ease"] * (10 - eff)
           + SCORE_WEIGHTS["brevity"] * brevity(minutes))
    return round(raw * 10, 1)


# ---------------------------------------------------------------------------
# XP: what finishing something is worth
# ---------------------------------------------------------------------------
# The score already says what a task deserves, so it is also what the task
# pays. One number, not two: nothing extra to learn, and nothing to farm —
# the only way to earn more is to do the work that was worth more. Finishing
# a hard, urgent, important thing lands a bigger number than clearing three
# more trivia off the list, which is the whole point.
#
# Levels sit steadily further apart — 100 XP to reach level 2, another 200 to
# reach 3, another 300 to reach 4 — so the bar never stops moving, but the
# first few come fast enough to be worth having at all.
XP_PER_LEVEL = 100


def task_xp(score: float) -> int:
    """What one finished task pays out: its score, rounded.

    Never nothing. A task with no impact, no deadline and no estimate still
    scores something, and a payout of zero would read as "that didn't count"
    for work that was, in fact, done.
    """
    return max(1, int(round(score)))


def level_threshold(level: int) -> int:
    """Total XP needed to have reached `level`. Everyone starts at level 1."""
    level = max(1, int(level))
    return XP_PER_LEVEL * (level - 1) * level // 2


def level_progress(total_xp: int) -> dict:
    """Where a running XP total puts you, and how far into the level it is.

    `progress` is the 0-1 fill of the bar; `into_level` and `level_span` are
    the same thing as numbers, because "180 / 300" says something the bar
    alone doesn't.
    """
    total = max(0, int(total_xp))
    level = 1
    while total >= level_threshold(level + 1):
        level += 1
    floor = level_threshold(level)
    span = level_threshold(level + 1) - floor
    into = total - floor
    return {
        "total": total,
        "level": level,
        "into_level": into,
        "level_span": span,
        "to_next": span - into,
        "progress": round(into / span, 4),
    }


def sort_mode(settings: dict) -> tuple[str, bool]:
    """(field, descending) — how the list is currently being read.

    `manual_order` predates the sorter and is still the flag a drag sets, so a
    list left on "smart" with that flag on is a manual list: nobody who
    arranged their tasks by hand should have them rearranged by an upgrade.
    """
    field = str(settings.get("sort_field") or "smart").strip().lower()
    if field not in SORT_FIELDS:
        field = "smart"
    if field == "smart" and settings.get("manual_order"):
        field = "manual"
    direction = str(settings.get("sort_dir") or "").strip().lower()
    if direction not in ("asc", "desc"):
        direction = DEFAULT_SORT_DIR[field]
    return field, direction == "desc"


def _epoch(iso: str | None) -> float | None:
    dt = parse_dt(iso)
    return dt.timestamp() if dt else None


def smart_sort_key(task: dict, d: dict) -> list:
    """The app's own opinion: most urgent first, quick wins ahead of slogs."""
    return [-d["urgency"], QUADRANT_RANK.get(d["quadrant"], 2),
            task["order_index"], task["created_at"]]


def manual_sort_key(task: dict, d: dict) -> list:
    """Where you put it, and nothing else."""
    return d.get("order_path", [task["order_index"]])


def list_sort_key(task: dict, d: dict, field: str, descending: bool) -> list:
    """The order the list is drawn in, as a key the page can compare directly.

    Every one-field sort ends in the same tie-breakers as the rest of the app
    (hand-arranged position, then age), so tasks the field cannot tell apart —
    two unscored tasks, two things due the same minute — still come out in a
    stable, sensible order instead of shuffling on every reload.
    """
    if field == "manual":
        return manual_sort_key(task, d)
    if field == "score":
        value, missing = d["score"], False
    elif field == "subtasks":
        value, missing = float(d["open_subtasks"]), False
    elif field == "deadline":
        # A container is read off the work it holds here too, so the list
        # sorts by the date shown on the task rather than an empty shell's.
        stamp = _epoch(d["rollup_deadline"] if d["has_subtasks"] else d["deadline"])
        value, missing = (stamp or 0.0), stamp is None
    elif field == "created":
        value, missing = (_epoch(task["created_at"]) or 0.0), False
    else:
        return smart_sort_key(task, d)
    # Undated tasks sink to the bottom whichever way the sort is pointing:
    # flipping the direction to see the far end of your deadlines should not
    # fill the top of the list with tasks that have no deadline at all.
    return [1 if missing else 0, -value if descending else value,
            task["order_index"], task["created_at"]]


def compute(tasks: list[dict], settings: dict, ratios: list[float] | None = None,
            now: datetime | None = None,
            planner: DayPlanner | None = None) -> dict[str, dict]:
    """Compute all derived fields for a flat task list.

    Returns {task_id: {buffered_estimate, quadrant, urgency, deadline,
    deadline_source, order_path, sort_key, list_sort_key, actionable}}.

    `sort_key` is the app's opinion of what comes first — urgency, then
    quadrant — unless the `manual_order` setting is on, in which case it is
    the position you dragged the task to and nothing else. It is what "what
    next" reads (see `next_task`).

    `list_sort_key` is the order the list is *drawn* in, which is the same
    thing until you pick a field in the sorter. Keeping the two apart means
    reading your list by deadline for a minute doesn't change what Taskmaster
    hands you next.

    `planner` is the book of what the days already hold. Pass one shared
    across every project and auto-deadlines are spread over days that have
    room instead of stacking; leave it out and one is made for this call,
    which still spreads *this* list but knows nothing about the other tabs.
    """
    now = now or datetime.now(timezone.utc)
    ratios = ratios or []
    buf = effective_buffer(settings, ratios)
    threshold = int(settings.get("matrix_threshold", 5))
    auto_deadlines = bool(settings.get("auto_deadlines", True))
    spread = bool(settings.get("spread_tasks", True))
    planner = planner if planner is not None else day_planner(settings)
    sort_field, sort_desc = sort_mode(settings)

    by_id = {t["id"]: t for t in tasks}
    children: dict[str | None, list[dict]] = {}
    for t in tasks:
        children.setdefault(t["parent_id"], []).append(t)
    for sibs in children.values():
        sibs.sort(key=lambda t: (t["order_index"], t["created_at"]))

    derived: dict[str, dict] = {}
    for t in tasks:
        derived[t["id"]] = {
            "buffered_estimate": buffered_estimate(t["estimated_time"], buf),
            "quadrant": quadrant(t["impact"], t["effort"], threshold),
            "buffer_applied": buf,
        }
    lengths = {t["id"]: derived[t["id"]]["buffered_estimate"] for t in tasks}

    def auto_plan(task: dict) -> tuple[datetime, int, datetime, datetime | None]:
        """(target, minutes, not_before, pin) for a top-level task the app is
        scheduling itself. `minutes` is 0 when there is nothing left to book.

        Two ways a task gets a day. A **start time** answers the question
        outright — this begins at seven, so book seven — and doubles as the
        floor, because a preference about when to start is not a licence to
        start in the past. Without one the app falls back to the quadrant
        horizon it has always used: a day, computed from when the task was
        written down, with the hour left to the planner.
        """
        start = parse_dt(task.get("start_at"))
        minutes = _tree_minutes(task, children, lengths)
        if start is not None:
            return start, minutes, max(now, start), start
        created = parse_dt(task["created_at"]) or now
        days = HORIZON_DAYS.get(derived[task["id"]]["quadrant"], 3)
        return created + timedelta(days=days), minutes, now, None

    def resolve_deadline(task: dict, parent_deadline: datetime | None) -> tuple[datetime | None, str]:
        user_dl = parse_dt(task["deadline"])
        if user_dl:
            # A deadline you set is the answer, and a start time alongside it
            # is then an intention rather than a second opinion about the
            # date: the block still ends where you said it must. It is not
            # ignored — it is what `urgency` reads to know the hour has come.
            return user_dl, "user"
        if not auto_deadlines:
            return None, "none"
        if parent_deadline:
            # Backward scheduling: this child must finish early enough to leave
            # room for the buffered estimates of every later sibling. A start
            # time on a step is not consulted here — the parent's slot is the
            # plan, and the steps tile it — but it still counts toward the
            # step's urgency, so "not until Thursday" is heard either way.
            sibs = children.get(task["parent_id"], [])
            idx = next(i for i, s in enumerate(sibs) if s["id"] == task["id"])
            tail_min = sum(
                (derived[s["id"]]["buffered_estimate"] or DEFAULT_ESTIMATE_MIN)
                for s in sibs[idx + 1:]
                if s["status"] in ACTIVE_STATUSES
            )
            return parent_deadline - timedelta(minutes=tail_min), "auto"
        target, minutes, floor, pin = auto_plan(task)
        if not minutes:
            return target, "auto"  # nothing left to do in here, nothing to book
        if not spread:
            # No day book to consult, so a start time is simply honoured:
            # begin then, finish a buffered estimate later.
            return (pin + timedelta(minutes=minutes)) if pin else target, "auto"
        # The horizon (or the start time) says which day this ought to land on.
        # The planner says where in that day it actually fits — or, when the
        # day is already spoken for, which of the following ones has room. The
        # whole tree is placed as one span: its steps are backward-scheduled
        # inside it just below, so the family occupies exactly the slot booked
        # for it.
        return planner.place(task["id"], target, minutes,
                             not_before=floor, pin=pin), "auto"

    def prebook() -> None:
        """Hand out the days before anything is drawn, best claim first.

        Placement used to follow whatever order the list happened to be in, so
        the first thing you ever typed got first pick of the afternoon and
        something that needed to happen in an hour queued up behind it. Now
        every top-level task the app is about to schedule is ranked, and they
        take their slots in that order — which is what makes a start time
        *push*: two things wanting the same day are separated by what they are
        worth, so dinner takes the evening and the game that can wait moves to
        whatever is left.

        The ranking is the ordinary priority score, with start pressure
        standing in for the deadline pressure nothing has yet. A task with no
        start time sits at neutral, so this changes nothing for a list that
        never uses the feature beyond breaking same-day ties by score.

        Fixed points go in first, exactly as `reserve_fixed` does across every
        project. Both are memoised on the task id, so when a shared planner has
        already been through this list all of it is a no-op.
        """
        roots = children.get(None, [])
        for task in roots:
            dl = parse_dt(task["deadline"])
            if dl is None:
                continue
            minutes = _tree_minutes(task, children, lengths)
            if minutes:
                planner.reserve(task["id"], dl, minutes)
        if not auto_deadlines:
            return
        queue = []
        for i, task in enumerate(roots):
            if parse_dt(task["deadline"]) is not None:
                continue
            target, minutes, floor, pin = auto_plan(task)
            if not minutes:
                continue
            rank = priority_score(
                task["impact"], task["effort"],
                start_pressure(pin, now) if pin is not None else float(NEUTRAL_SCORE),
                minutes)
            queue.append((planner.local_day(target), -rank, i,
                          task["id"], target, minutes, floor, pin))
        for _, _, _, key, target, minutes, floor, pin in sorted(queue):
            planner.place(key, target, minutes, not_before=floor, pin=pin)

    def walk(parent_id: str | None, parent_deadline: datetime | None,
             prefix: tuple[int, ...]) -> None:
        for i, task in enumerate(children.get(parent_id, [])):
            dl, source = resolve_deadline(task, parent_deadline)
            if spread and parent_id is None and source == "user" and dl:
                # A deadline you set is a fixed point, and the day it lands on
                # is that much fuller for everything placed around it after.
                minutes = _tree_minutes(task, children, lengths)
                if minutes:
                    planner.reserve(task["id"], dl, minutes)
            d = derived[task["id"]]
            d["deadline"] = dl.isoformat(timespec="seconds") if dl else None
            d["deadline_source"] = source
            # Where this task sits in the hand-arranged tree, top down. Two
            # of these compare exactly the way the list reads: [0] < [0, 1]
            # (a task before its own subtasks) < [1].
            d["order_path"] = [*prefix, i]
            if task["status"] in ACTIVE_STATUSES:
                d["urgency"] = urgency(dl, d["buffered_estimate"], now,
                                       parse_dt(task.get("start_at")))
            else:
                d["urgency"] = 0.0
            walk(task["id"], dl, (*prefix, i))

    if spread:
        prebook()
    walk(None, None, ())

    # Roll subtree totals up: a parent's real cost is the sum of everything
    # underneath it, and its real deadline is the furthest one inside it.
    for t in tasks:
        if t["parent_id"] not in by_id:
            _rollup(t, children, derived)
            _count_open_subtasks(t, children, derived)

    for t in tasks:
        d = derived[t["id"]]
        open_children = [
            c for c in children.get(t["id"], []) if c["status"] in ACTIVE_STATUSES
        ]
        d["actionable"] = t["status"] in ACTIVE_STATUSES and not open_children
        # Containers are judged on the work they still hold, not their own
        # (usually meaningless) estimate and deadline.
        if d["has_subtasks"] and t["status"] in ACTIVE_STATUSES:
            d["urgency"] = urgency(parse_dt(d["rollup_deadline"]),
                                   d["rollup_remaining"], now,
                                   parse_dt(t.get("start_at")))
        # Length before score: how long a task takes is one of the four things
        # the score is made of, and for a container that is the work inside it.
        d["length_min"] = block_length(d)
        d["raw_length_min"] = max(1, round(d["length_min"] / (1.0 + buf)))
        # Computed after urgency, so containers score off what they actually
        # still hold rather than off their own empty shell. Containers then
        # have this replaced outright by the pass below.
        d["score"] = priority_score(t["impact"], t["effort"], d["urgency"],
                                    d["length_min"])

    # A parent is its parts. Bottom-up, so a container at any depth ends up
    # holding the combined score of the work underneath it.
    for t in tasks:
        if t["parent_id"] not in by_id:
            _rollup_score(t, children, derived)

    # Sort keys last of all: two of them read the score, which is only final
    # once the rollup above has been through the whole tree.
    for t in tasks:
        d = derived[t["id"]]
        # Manual order means manual order: once you have arranged the list by
        # hand, the app stops second-guessing you and reads it top to bottom.
        d["sort_key"] = (manual_sort_key(t, d) if sort_field == "manual"
                         else smart_sort_key(t, d))
        d["list_sort_key"] = list_sort_key(t, d, sort_field, sort_desc)
    return derived


def _rollup_score(task: dict, children: dict, derived: dict) -> tuple[float, int]:
    """Replace every container's score with the combined score of its subtask
    tree, and return that subtree as (score x minutes, minutes).

    A parent of subtasks has no score of its own to give. Whatever impact and
    effort someone once put on "Move house" describes nothing you can sit down
    and do; the work is in the steps, and so is the number. So a container is
    scored as the weighted mean of the leaves beneath it, each weighing what
    it costs in minutes — which makes the parent read as exactly what its
    remaining afternoon is worth, and moves it up and down as the steps inside
    it are scored, finished, or dropped.

    Leaves only, the same rule the rest of the module books time by: a
    container adds no minutes of its own, so nothing is counted twice. Work
    already done or discarded weighs nothing either — a project is worth what
    is left of it, not what it once was. A container whose subtasks are all
    finished has nothing left to inherit from and keeps its own score.
    """
    kids = [c for c in children.get(task["id"], []) if c["status"] != "discarded"]
    weighted, minutes = 0.0, 0
    for kid in kids:
        kid_weighted, kid_minutes = _rollup_score(kid, children, derived)
        weighted += kid_weighted
        minutes += kid_minutes
    d = derived[task["id"]]
    if kids:
        if minutes:
            d["score"] = round(weighted / minutes, 1)
        return weighted, minutes
    if task["status"] not in ACTIVE_STATUSES:
        return 0.0, 0
    return d["score"] * d["length_min"], d["length_min"]


def next_task(tasks: list[dict], derived: dict[str, dict]) -> dict | None:
    """The single task Taskmaster should surface next: highest urgency first,
    quick wins break ties, thankless work sinks.

    Under manual order the same call returns the first actionable task in the
    order you arranged, because that is what the list now means.
    """
    candidates = [t for t in tasks if derived[t["id"]]["actionable"]]
    if not candidates:
        return None
    candidates.sort(key=lambda t: derived[t["id"]]["sort_key"])
    return candidates[0]


def _rollup(task: dict, children: dict, derived: dict) -> None:
    """Fill rollup_* on a task and, depth-first, everything under it.

    A task that contains subtasks is a container: the number that matters is
    the sum of what it holds, not the estimate someone put on the container
    itself. `rollup_estimate` is the whole subtree, `rollup_done` the part
    already finished, `rollup_remaining` what is actually left to do.
    Discarded branches drop out of the plan entirely.
    """
    # Recurse into every child so all of them get rollup fields, then count
    # only the ones still in the plan.
    for kid in children.get(task["id"], []):
        _rollup(kid, children, derived)
    kids = [c for c in children.get(task["id"], []) if c["status"] != "discarded"]
    d = derived[task["id"]]
    d["has_subtasks"] = bool(kids)

    own = d["buffered_estimate"]
    own_dl, own_src = d.get("deadline"), d.get("deadline_source", "none")

    if not kids:
        total = own
        d["rollup_estimate"] = total
        d["rollup_done"] = total if (total and task["status"] == "done") else 0
        d["rollup_remaining"] = 0 if task["status"] not in ACTIVE_STATUSES else (total or 0)
        d["rollup_deadline"] = own_dl
        d["rollup_deadline_source"] = own_src
        return

    total = 0
    known = False
    done = 0
    for kid in kids:
        kd = derived[kid["id"]]
        if kd["rollup_estimate"] is not None:
            total += kd["rollup_estimate"]
            known = True
        done += kd["rollup_done"]
    if not known:
        # Nothing underneath has an estimate yet — fall back to the container's.
        total = own
        done = total if (total and task["status"] == "done") else 0
    elif task["status"] == "done":
        done = total

    d["rollup_estimate"] = total
    d["rollup_done"] = min(done, total) if total is not None else 0
    d["rollup_remaining"] = max(0, (total or 0) - d["rollup_done"])

    # Furthest deadline anywhere in the subtree, the container's own included.
    best_dl, best_src = own_dl, own_src
    for kid in kids:
        kd = derived[kid["id"]]
        cand, cand_src = kd.get("rollup_deadline"), kd.get("rollup_deadline_source", "none")
        if cand and (best_dl is None or parse_dt(cand) > parse_dt(best_dl)):
            best_dl, best_src = cand, cand_src
    d["rollup_deadline"] = best_dl
    d["rollup_deadline_source"] = best_src


def _count_open_subtasks(task: dict, children: dict, derived: dict) -> None:
    """Fill `open_subtasks` on a task and everything under it.

    Counted all the way down and only for work still to do: "12 subtasks" on a
    task you have half finished is a number about the past. Sorting by it
    answers "which of these is still a whole project?", which is the reason to
    sort by it at all.
    """
    total = 0
    for kid in children.get(task["id"], []):
        _count_open_subtasks(kid, children, derived)
        if kid["status"] == "discarded":
            continue  # a dropped branch is off the plan, and so is its inside
        total += derived[kid["id"]]["open_subtasks"]
        if kid["status"] in ACTIVE_STATUSES:
            total += 1
    derived[task["id"]]["open_subtasks"] = total


def _children_map(tasks: list[dict]) -> dict[str | None, list[dict]]:
    children: dict[str | None, list[dict]] = {}
    for t in tasks:
        children.setdefault(t["parent_id"], []).append(t)
    for sibs in children.values():
        sibs.sort(key=lambda t: (t["order_index"], t["created_at"]))
    return children


def focus_queue(tasks: list[dict], root_id: str) -> list[dict]:
    """The order Taskmaster walks a task tree: depth-first, children before
    their parent.

    You cannot finish a parent before the things it is made of, so the queue
    descends to the deepest first step, works along that level, then surfaces
    to the parent as its wrap-up step, and so on up to the root.
    """
    children = _children_map(tasks)
    by_id = {t["id"]: t for t in tasks}
    root = by_id.get(root_id)
    if not root:
        return []

    out: list[dict] = []

    def visit(task: dict) -> None:
        if task["status"] not in ACTIVE_STATUSES:
            return  # done/discarded branches are behind us
        for kid in children.get(task["id"], []):
            visit(kid)
        out.append(task)

    visit(root)
    return out


def focus_root_id(tasks: list[dict], derived: dict[str, dict]) -> str | None:
    """Which tree to work on next: the top-level task that owns the most
    urgent actionable step. Urgency picks the project; tree order (see
    `focus_queue`) picks the step inside it."""
    nxt = next_task(tasks, derived)
    if not nxt:
        return None
    by_id = {t["id"]: t for t in tasks}
    cursor = nxt
    seen = set()
    while cursor.get("parent_id") and cursor["parent_id"] in by_id:
        if cursor["id"] in seen:
            break
        seen.add(cursor["id"])
        cursor = by_id[cursor["parent_id"]]
    return cursor["id"]


def ancestor_titles(tasks: list[dict], task: dict) -> list[str]:
    """Titles from the top-level ancestor down to (but excluding) the task."""
    by_id = {t["id"]: t for t in tasks}
    path: list[str] = []
    cursor = task
    seen = set()
    while cursor.get("parent_id") and cursor["parent_id"] in by_id:
        if cursor["id"] in seen:
            break
        seen.add(cursor["id"])
        cursor = by_id[cursor["parent_id"]]
        path.insert(0, cursor["title"])
    return path


def nudge_plan(tasks: list[dict], derived: dict[str, dict], task_id: str,
               new_deadline: datetime) -> dict[str, dict[str, str]]:
    """The fields to write when a past-due task is pushed to a new date.

    Returns {task_id: {"deadline": iso, "start_at": iso}}, the start time only
    where the task has one. The task itself lands exactly on `new_deadline`;
    every task nested under it that carries a deadline *you* set slides by the
    same delta. That is what "the same length" means for anything bigger than
    one step: a plan spread over three days stays spread over three days
    instead of collapsing onto the new date. Auto-assigned deadlines are left
    alone — they are recomputed by backward scheduling from the new date on
    the very next read, which is the same shift by another route.

    Start times slide with the deadlines, for the same reason and one more: a
    task moved to tomorrow that still says it should have begun this morning
    reads as urgent forever, because a start time that has gone by is exactly
    what the scheduler treats as "you should be doing this now".

    A task with no deadline at all (auto-deadlines off, nothing set) has no
    delta to apply, so only the task itself is scheduled.
    """
    by_id = {t["id"]: t for t in tasks}
    task = by_id.get(task_id)
    if task is None:
        return {}
    # Deadlines are stored to the second, so the shift is worked out to the
    # second too — otherwise a stray microsecond on one end of the subtraction
    # rounds a subtask a second away from where the plan put it.
    new_deadline = new_deadline.replace(microsecond=0)
    moves: dict[str, dict[str, str]] = {
        task_id: {"deadline": new_deadline.isoformat(timespec="seconds")}
    }

    current = parse_dt(derived.get(task_id, {}).get("deadline"))
    if current is None:
        return moves
    delta = new_deadline - current.replace(microsecond=0)

    def slid(value: str | None) -> str | None:
        when = parse_dt(value)
        if when is None:
            return None
        return (when.replace(microsecond=0) + delta).isoformat(timespec="seconds")

    own_start = slid(task.get("start_at"))
    if own_start:
        moves[task_id]["start_at"] = own_start

    children: dict[str | None, list[dict]] = {}
    for t in tasks:
        children.setdefault(t["parent_id"], []).append(t)
    stack = list(children.get(task_id, []))
    while stack:
        kid = stack.pop()
        stack.extend(children.get(kid["id"], []))
        fields = {k: v for k, v in
                  (("deadline", slid(kid["deadline"])),
                   ("start_at", slid(kid.get("start_at")))) if v}
        if fields:
            moves[kid["id"]] = fields
    return moves


# ---------------------------------------------------------------------------
# Recurrence: work that comes back
# ---------------------------------------------------------------------------
# Some things are not tasks, they are rhythms — bins on Tuesday, rent on the
# first, the standup every weekday morning. Retyping them is the friction that
# makes people stop using a list at all, and leaving one ticked-off task lying
# there as a reminder is worse: it is a lie about what is still to do.
#
# Four shapes cover essentially everything a person actually repeats — daily,
# weekly, monthly, yearly — so those are the presets. "Custom" is not a fifth
# kind of rule: it is the same four with their knobs exposed (every N of them,
# on chosen weekdays, on the last Friday rather than the 12th). One rule
# format, four entry points into it, which means one thing to test and one
# thing to explain.
#
# Everything here is pure: a rule plus the instant of the previous occurrence
# in, the instant of the next one out. Days, weekdays and "the 1st" are all
# local ideas, so the arithmetic happens in the local zone and hands back UTC
# instants, exactly like the day planner above.

RECUR_FREQS = ("daily", "weekly", "monthly", "yearly")
RECUR_MONTHLY_MODES = ("day_of_month", "nth_weekday")
RECUR_MAX_INTERVAL = 365       # "every 400 days" is a date, not a rhythm
RECUR_MAX_COUNT = 1000
RECUR_MAX_LEAD_DAYS = 30       # how far ahead an occurrence may be materialized
RECUR_DEFAULT_LEAD_DAYS = 1    # tomorrow's copy shows up tonight
# Weekdays are 0=Sunday..6=Saturday, matching JavaScript's `Date.getDay()` and
# the `week_start` setting, so the page never has to translate.
WEEKDAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")
# Weekly candidates are scanned a day at a time; this is the ceiling on that
# walk, generous enough for "every 52 weeks on a Tuesday" and finite enough
# that a malformed rule can never spin.
RECUR_SCAN_DAYS = 366 * 2
RECUR_SCAN_MONTHS = 12 * 20


def _js_weekday(d: date) -> int:
    """0=Sunday..6=Saturday — the convention the whole app speaks."""
    return (d.weekday() + 1) % 7


def _month_len(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + months
    return index // 12, index % 12 + 1


def _parse_time(value) -> tuple[int, int] | None:
    """"HH:MM" → (hour, minute). Anything unusable is simply no opinion."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_date(value) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def normalize_rule(raw: dict | None) -> dict | None:
    """Clean a rule from the page into the one shape everything else reads.

    Rules arrive from a UI, get stored as JSON, and are then trusted by the
    date arithmetic below, so this is the only place that has to be paranoid:
    every field is clamped to something the maths can survive, and anything
    nonsensical falls back to the sane default rather than raising. A rule the
    app cannot understand is `None` — "this does not repeat" — because the one
    outcome worse than the wrong repeat is a task that will not save.
    """
    if not isinstance(raw, dict):
        return None
    freq = str(raw.get("freq") or "").strip().lower()
    if freq not in RECUR_FREQS:
        return None

    rule: dict = {"freq": freq}
    try:
        interval = int(raw.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 1
    rule["interval"] = max(1, min(RECUR_MAX_INTERVAL, interval))

    weekdays: list[int] = []
    for day in (raw.get("weekdays") or []):
        try:
            day = int(day)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in weekdays:
            weekdays.append(day)
    rule["weekdays"] = sorted(weekdays) if freq == "weekly" else []

    mode = str(raw.get("monthly_mode") or "day_of_month").strip().lower()
    rule["monthly_mode"] = mode if mode in RECUR_MONTHLY_MODES else "day_of_month"

    month_day = raw.get("month_day")
    try:
        month_day = int(month_day) if month_day is not None else None
    except (TypeError, ValueError):
        month_day = None
    # -1 means "the last day", whatever length the month turns out to be.
    if month_day is not None and not (month_day == -1 or 1 <= month_day <= 31):
        month_day = None
    rule["month_day"] = month_day

    nth = raw.get("nth")
    try:
        nth = int(nth) if nth is not None else None
    except (TypeError, ValueError):
        nth = None
    if nth is not None and not (nth == -1 or 1 <= nth <= 5):
        nth = None
    rule["nth"] = nth

    weekday = raw.get("weekday")
    try:
        weekday = int(weekday) if weekday is not None else None
    except (TypeError, ValueError):
        weekday = None
    rule["weekday"] = weekday if weekday is not None and 0 <= weekday <= 6 else None

    rule["time"] = None
    parsed_time = _parse_time(raw.get("time"))
    if parsed_time:
        rule["time"] = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"

    count = raw.get("count")
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None
    rule["count"] = max(1, min(RECUR_MAX_COUNT, count)) if count is not None else None

    until = _parse_date(raw.get("until"))
    rule["until"] = until.isoformat() if until else None

    # A rule only carries the fields its frequency actually uses. The page
    # sends every control it has, whichever ones are on screen, and a stored
    # weekly rule that also remembers "the 3rd Thursday" would come back and
    # repopulate a dialog with a monthly answer nobody gave.
    if freq not in ("monthly", "yearly"):
        rule["monthly_mode"] = "day_of_month"
        rule["month_day"] = rule["nth"] = rule["weekday"] = None
    elif rule["monthly_mode"] == "nth_weekday":
        rule["month_day"] = None
    else:
        rule["nth"] = rule["weekday"] = None

    rule["from_completion"] = bool(raw.get("from_completion"))

    try:
        lead = int(raw.get("lead_days", RECUR_DEFAULT_LEAD_DAYS))
    except (TypeError, ValueError):
        lead = RECUR_DEFAULT_LEAD_DAYS
    rule["lead_days"] = max(0, min(RECUR_MAX_LEAD_DAYS, lead))
    return rule


def _ordinal(n: int) -> str:
    if n == -1:
        return "last"
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " & " + names[-1]


def describe_rule(rule: dict | None, anchor: datetime | None = None,
                  tz: tzinfo | None = None) -> str:
    """The rule in words, for the badge on the task and the repeat dialog.

    Computed on the server and shipped with the task so the badge, the dialog
    and the calendar cannot drift into describing the same rule three
    different ways. `anchor` fills in whatever the rule leaves implicit — a
    monthly rule with no day repeats on the day the series started — so the
    sentence says what will actually happen rather than what was typed.
    """
    rule = normalize_rule(rule)
    if not rule:
        return ""
    tz = tz or timezone.utc
    local = anchor.astimezone(tz) if anchor else None
    n = rule["interval"]
    freq = rule["freq"]

    if freq == "daily":
        head = "every day" if n == 1 else f"every {n} days"
    elif freq == "weekly":
        days = rule["weekdays"] or ([_js_weekday(local.date())] if local else [])
        names = _join([WEEKDAY_NAMES[d] for d in sorted(days)])
        every = "every week" if n == 1 else f"every {n} weeks"
        head = f"{every} on {names}" if names else every
        # "every week on Mon, Wed & Fri" is what a week of standups looks like;
        # a bare "every week" only happens before a day has been chosen.
    else:
        every = ("every month" if freq == "monthly" else "every year") if n == 1 else (
            f"every {n} months" if freq == "monthly" else f"every {n} years")
        if rule["monthly_mode"] == "nth_weekday":
            nth = rule["nth"] if rule["nth"] is not None else (
                _nth_of_month(local.date()) if local else 1)
            weekday = rule["weekday"] if rule["weekday"] is not None else (
                _js_weekday(local.date()) if local else 0)
            head = f"{every} on the {_ordinal(nth)} {WEEKDAY_NAMES[weekday]}"
        else:
            day = rule["month_day"] if rule["month_day"] is not None else (
                local.day if local else 1)
            head = f"{every} on the {_ordinal(day)}"
        if freq == "yearly" and local:
            head += f" of {MONTH_NAMES[local.month - 1]}"

    bits = [head]
    if rule["time"]:
        bits.append(f"at {rule['time']}")
    if rule["from_completion"]:
        bits.append("counted from when you finish it")
    if rule["count"]:
        bits.append(f"{rule['count']} times")
    if rule["until"]:
        until = _parse_date(rule["until"])
        bits.append(f"until {until.strftime('%-d %b %Y')}" if until else "")
    return " · ".join(b for b in bits if b)


def _nth_of_month(d: date) -> int:
    """Which Tuesday of the month a date is: 1-5, counting from the start."""
    return (d.day - 1) // 7 + 1


def _nth_weekday_date(year: int, month: int, weekday: int, nth: int) -> date | None:
    """The nth `weekday` of a month, or None when the month has no fifth one.

    -1 is the last, which every month has. A missing fifth Friday returns
    nothing rather than sliding into the next month: "the 5th Friday" of a
    month that has four is a month this rule skips, which is what anyone
    picking it means.
    """
    length = _month_len(year, month)
    matches = [day for day in range(1, length + 1)
               if _js_weekday(date(year, month, day)) == weekday]
    if not matches:
        return None
    if nth == -1:
        return date(year, month, matches[-1])
    if 1 <= nth <= len(matches):
        return date(year, month, matches[nth - 1])
    return None


def _month_day_date(year: int, month: int, day: int) -> date:
    """A day-of-month, clamped to months that are too short to hold it.

    The 31st in a 30-day month becomes the 30th, and -1 is always the last
    day. Clamping (rather than skipping) is what "monthly on the 31st" means
    to the person who set it: a bill due at the end of the month is due at the
    end of every month, February included.
    """
    length = _month_len(year, month)
    if day == -1:
        return date(year, month, length)
    return date(year, month, min(day, length))


def _at_time(day: date, rule: dict, anchor_local: datetime, tz: tzinfo) -> datetime:
    """A local date plus the rule's time of day, as a UTC instant."""
    fixed = _parse_time(rule.get("time"))
    hour, minute = fixed if fixed else (anchor_local.hour, anchor_local.minute)
    return datetime.combine(day, time(hour, minute), tzinfo=tz).astimezone(timezone.utc)


def next_occurrence(rule: dict | None, after: datetime,
                    anchor: datetime | None = None,
                    tz: tzinfo | None = None) -> datetime | None:
    """The first occurrence strictly after `after`, or None once the rule ends.

    `anchor` is the instant the series is phased from — its first occurrence —
    and is what makes "every 3 weeks" mean the same three weeks forever
    instead of drifting each time an occurrence is generated. It defaults to
    `after`, which is the right answer for a rule counted from completion:
    "three days after you finish" is phased on finishing.

    Returns UTC. `until` is honoured here (an occurrence past it is no
    occurrence at all); `count` is the series' business, since only it knows
    how many have actually been handed out.
    """
    rule = normalize_rule(rule)
    if not rule:
        return None
    tz = tz or timezone.utc
    anchor = anchor or after
    anchor_local = anchor.astimezone(tz)
    after_local = after.astimezone(tz)
    limit = _parse_date(rule["until"])

    def ship(day: date) -> datetime | None:
        if limit and day > limit:
            return None
        return _at_time(day, rule, anchor_local, tz)

    freq, step = rule["freq"], rule["interval"]

    if freq == "daily":
        base = anchor_local.date()
        gap = (after_local.date() - base).days
        k = max(0, gap // step)
        for _ in range(RECUR_SCAN_DAYS):
            candidate = base + timedelta(days=k * step)
            when = ship(candidate)
            if when is None:
                return None
            if when > after:
                return when
            k += 1
        return None

    if freq == "weekly":
        days = rule["weekdays"] or [_js_weekday(anchor_local.date())]
        # Phase is measured in whole weeks from the anchor's week, on an
        # ISO (Monday-based) grid. Which day the *calendar* starts on is a
        # display preference and has no business shifting a repeat.
        base_week = anchor_local.date() - timedelta(days=anchor_local.date().weekday())
        cursor = after_local.date()
        for _ in range(RECUR_SCAN_DAYS):
            if _js_weekday(cursor) in days:
                week = cursor - timedelta(days=cursor.weekday())
                if ((week - base_week).days // 7) % step == 0:
                    when = ship(cursor)
                    if when is None:
                        return None
                    if when > after:
                        return when
            cursor += timedelta(days=1)
        return None

    # monthly and yearly are the same walk over months, a year being twelve
    # of them: step forward, resolve the day inside the month, keep the first
    # result that is actually in the future.
    months_per_step = step if freq == "monthly" else step * 12
    base_year, base_month = anchor_local.year, anchor_local.month
    gap = (after_local.year * 12 + after_local.month) - (base_year * 12 + base_month)
    k = max(0, gap // months_per_step)
    for _ in range(RECUR_SCAN_MONTHS):
        year, month = _add_months(base_year, base_month, k * months_per_step)
        if rule["monthly_mode"] == "nth_weekday":
            nth = rule["nth"] if rule["nth"] is not None else _nth_of_month(anchor_local.date())
            weekday = (rule["weekday"] if rule["weekday"] is not None
                       else _js_weekday(anchor_local.date()))
            day = _nth_weekday_date(year, month, weekday, nth)
        else:
            wanted = rule["month_day"] if rule["month_day"] is not None else anchor_local.day
            day = _month_day_date(year, month, wanted)
        k += 1
        if day is None:
            continue  # a month with no fifth Friday is a month this rule skips
        if limit and day > limit:
            return None
        when = _at_time(day, rule, anchor_local, tz)
        if when > after:
            return when
    return None
