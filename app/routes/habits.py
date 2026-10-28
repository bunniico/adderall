"""The Habits tab: routines, their streaks and their year of squares. See `habits.py`."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, habits, logic, modules

router = APIRouter(dependencies=[Depends(modules.require("habits"))])


class HabitRule(BaseModel):
    """A routine's frequency, as loosely as the page cares to phrase it.

    Validated rather than typed: the three shapes share one field set and
    `habits.normalize_rule` is the one place that decides what a rule means,
    so a rule cannot be legal here and illegal there.
    """
    type: str = "daily"
    days: list[int] | None = None
    times: int | None = None


class HabitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = habits.DEFAULT_CATEGORY
    rule: HabitRule = Field(default_factory=HabitRule)


class HabitUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = None
    rule: HabitRule | None = None


class HabitCheck(BaseModel):
    """One day's tick. The day is local and the page knows which one that is —
    the server has no timezone of its own, the same arrangement every other
    date on the wire already runs on."""
    day: str | None = None
    done: bool = True


# ---------- habits ----------
# The routines you keep, what they add up to, and the year of squares that
# says how it has actually been going. Nothing here touches the planner: a
# routine has no estimate and no slot, so it can neither be late nor take up
# an afternoon. See `habits.py`.


def _today_local(settings: dict | None = None) -> date:
    """The local date the server should count a bare tick against.

    The page sends its own day with every check-in, because it is the side
    that knows the timezone for certain. This is the fallback for a request
    that didn't, and for the "is it done today" the payload answers on its own.
    """
    settings = settings or db.get_settings()
    return datetime.now(logic.resolve_tz(settings.get("timezone"))).date()


def _habit_row(habit: dict, days: list[str], today: date, week_start: int) -> dict:
    """One routine, with its ticks and everything derived from them.

    The check-ins ride along rather than being fetched per habit: the page
    draws a year of squares for each one, and a payload it has to assemble
    from a dozen round trips is a heatmap that arrives in pieces.
    """
    rule = habit["rule"]
    window_start, window_end = habits.heatmap_window(today, first_day=week_start)
    return {
        "id": habit["id"],
        "name": habit["name"],
        "category": habit["category"],
        "rule": rule,
        "rule_label": habits.rule_label(rule),
        "created_at": habit["created_at"],
        # Only the squares the calendar can draw. The stats above are counted
        # off every tick there has ever been — see `habits.stats`.
        "checkins": [d for d in days if window_start.isoformat() <= d
                     <= window_end.isoformat()],
        "stats": habits.stats(rule, days, today, week_start),
    }


def _habits() -> dict:
    """Every routine, grouped-ready and already counted."""
    settings = db.get_settings()
    today = _today_local(settings)
    week_start = int(settings.get("week_start") or 0)
    checkins = db.habit_checkins()
    rows = [_habit_row(h, checkins.get(h["id"], []), today, week_start)
            for h in db.list_habits()]

    window_start, window_end = habits.heatmap_window(today, first_day=week_start)
    due = [r for r in rows if r["stats"]["due_today"]]
    done = [r for r in due if r["stats"]["done_today"]]
    return {
        "habits": rows,
        "categories": list(habits.CATEGORIES),
        "today": today.isoformat(),
        "week_start": week_start,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        # The one line the tab is really for: how today is going. Counted over
        # what is actually due — a Sunday rest day is not a routine you failed.
        "today_due": len(due),
        "today_done": len(done),
        # A day everything was ticked off is worth saying out loud, and it is
        # not the same thing as having no routines at all.
        "today_clear": bool(due) and len(done) == len(due),
    }


# ---------- habits ----------
# Every route here answers with the whole Habits payload, the way the project
# routes answer with the whole page state: ticking a box changes a streak, a
# rate and a square, and a reply that carried only the box would leave the
# page to work the rest out for itself and disagree with the server about it.


def _require_habit(habit_id: str) -> dict:
    habit = db.get_habit(habit_id)
    if habit is None:
        raise HTTPException(404, "Routine not found")
    return habit


def _habit_category(value: str | None) -> str:
    category = (value or habits.DEFAULT_CATEGORY).strip()
    if category not in habits.CATEGORY_IDS:
        raise HTTPException(400, f"Unknown category: {category}")
    return category


def _habit_rule(rule: HabitRule | None) -> dict:
    try:
        return habits.normalize_rule(rule.model_dump() if rule else None)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _habit_day(day: str | None) -> str:
    """The local day a tick belongs to, as the page reported it.

    Trusted, within reason: the page knows its own timezone and the server
    does not, so the only thing worth checking is that it is a date at all —
    and that it is not one that hasn't happened yet, because a tick in the
    future is a typo rather than a plan.
    """
    if not day:
        return _today_local().isoformat()
    try:
        parsed = date.fromisoformat(day.strip())
    except ValueError:
        raise HTTPException(400, "A day looks like 2026-09-21")
    # A day's grace either side of the server's idea of today: whoever is
    # ticking is somewhere, and every zone on Earth is inside that window.
    if parsed > _today_local() + timedelta(days=1):
        raise HTTPException(400, "That day hasn't happened yet")
    return parsed.isoformat()


@router.get("/api/habits")
def get_habits():
    """The Habits tab: your routines, their streaks, and a year of squares."""
    return _habits()


@router.post("/api/habits", status_code=201)
def create_habit(body: HabitCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "A routine needs a name")
    db.create_habit(name, _habit_category(body.category), _habit_rule(body.rule))
    return _habits()


@router.patch("/api/habits/{habit_id}")
def update_habit(habit_id: str, body: HabitUpdate):
    """Rename a routine, move it to another part of your life, or change how
    often it wants doing. The ticks it already has are never touched — you did
    those days, whatever the rule says now."""
    _require_habit(habit_id)
    fields: dict = {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "A routine needs a name")
        fields["name"] = name
    if body.category is not None:
        fields["category"] = _habit_category(body.category)
    if body.rule is not None:
        fields["rule"] = _habit_rule(body.rule)
    db.update_habit(habit_id, fields)
    return _habits()


@router.delete("/api/habits/{habit_id}")
def delete_habit(habit_id: str):
    """Forget a routine and every tick it collected. There is no archive: a
    routine you have stopped keeping is not a thing to file away."""
    _require_habit(habit_id)
    db.delete_habit(habit_id)
    return _habits()


@router.post("/api/habits/{habit_id}/check")
def check_habit(habit_id: str, body: HabitCheck):
    """Tick — or untick — one day.

    Idempotent both ways, so the page sends the state it wants rather than a
    toggle it has to have guessed right. Untick matters as much as tick: a
    square you filled in by mistake that you cannot clear turns the calendar
    into something you distrust.
    """
    _require_habit(habit_id)
    db.set_checkin(habit_id, _habit_day(body.day), bool(body.done))
    return _habits()
