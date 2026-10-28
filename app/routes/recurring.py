"""Repeating tasks: the rule on a task, and the sweep that makes its copies.
See `recurring.py`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import core, db, logic, modules, recurring, scheduler

router = APIRouter(dependencies=[Depends(modules.require("recurring"))])


class RepeatRule(BaseModel):
    """How often a task comes back.

    Four shapes, not five: daily, weekly, monthly and yearly, each with an
    interval and — where it means something — a set of weekdays or a day of
    the month. "Custom" on the page is these same fields with their knobs
    turned up (every 3 weeks on Mon & Thu, the last Friday of every second
    month), which keeps one rule format to store, validate and test.

    Every field is re-clamped by `logic.normalize_rule` after this: pydantic
    guards the types, that guards the meaning.
    """
    freq: str = Field(pattern="^(daily|weekly|monthly|yearly)$")
    interval: int = Field(default=1, ge=1, le=logic.RECUR_MAX_INTERVAL)
    # 0=Sunday..6=Saturday — the same numbering as the calendar and JS.
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    monthly_mode: str = Field(default="day_of_month",
                              pattern="^(day_of_month|nth_weekday)$")
    month_day: int | None = Field(default=None, ge=-1, le=31)
    nth: int | None = Field(default=None, ge=-1, le=5)
    weekday: int | None = Field(default=None, ge=0, le=6)
    time: str | None = Field(default=None, max_length=5)
    count: int | None = Field(default=None, ge=1, le=logic.RECUR_MAX_COUNT)
    until: str | None = Field(default=None, max_length=32)
    from_completion: bool = False
    lead_days: int = Field(default=logic.RECUR_DEFAULT_LEAD_DAYS, ge=0,
                           le=logic.RECUR_MAX_LEAD_DAYS)


class RepeatPreview(RepeatRule):
    """A rule the page is still typing, plus the task it would be set on."""
    task_id: str | None = None


# ---------- recurring tasks ----------
# A repeating job is a `series`: the rule, a snapshot of what to copy, and
# where in the rhythm it has got to. The task on your list is one occurrence
# of it, and there is only ever one open at a time — see `recurring.py`.

@router.put("/api/tasks/{task_id}/repeat")
def set_repeat(task_id: str, body: RepeatRule):
    """Make a task repeat, or re-time a rhythm it already has.

    Only top-level tasks repeat. A subtask is a step *inside* something, and
    a step that came back on its own schedule while the thing containing it
    did not would be a plan nobody could read; repeat the task it belongs to
    and the steps come with it, because the copy is of the whole tree.
    """
    task = core.require_task(task_id)
    if task["parent_id"]:
        raise HTTPException(
            400, "Only a top-level task can repeat — its subtasks come with it")
    rule = logic.normalize_rule(body.model_dump())
    if not rule:
        raise HTTPException(400, "That repeat rule doesn't describe a schedule")
    settings = db.get_settings()
    existing = db.get_series(task["series_id"])
    if existing and existing["active"]:
        updated = recurring.change_rule(existing, rule, task, settings)
    else:
        updated = recurring.start_series(task, rule, settings)
    if not updated:
        raise HTTPException(400, "That repeat rule never comes round again")
    return core.state()


@router.delete("/api/tasks/{task_id}/repeat")
def clear_repeat(task_id: str):
    """Stop a job coming back. The copy on your list stays — it is a real
    task you may still mean to do; it just has no sequel."""
    task = core.require_task(task_id)
    if not task["series_id"]:
        raise HTTPException(404, "This task doesn't repeat")
    recurring.stop_series(task["series_id"])
    return core.state()


@router.post("/api/recurring/preview")
def preview_repeat(body: RepeatPreview):
    """What a rule would actually do, before anyone commits to it.

    Three or four real dates say what a rule means far better than the rule
    does — "the last Friday of every second month" is a sentence; *27 Feb,
    24 Apr, 26 Jun* is an answer. It is also the reason the page has no date
    arithmetic of its own: the dialog, the badge and the schedule all come
    from the one implementation in `logic.py`.
    """
    rule = logic.normalize_rule(body.model_dump(exclude={"task_id"}))
    if not rule:
        raise HTTPException(400, "That repeat rule doesn't describe a schedule")
    settings = db.get_settings()
    task = db.get_task(body.task_id) if body.task_id else None
    anchor = recurring.first_occurrence(rule, task, settings)
    dates = recurring.upcoming(rule, anchor, 4, settings)
    return {
        "summary": logic.describe_rule(rule, anchor,
                                       logic.resolve_tz(settings.get("timezone"))),
        "occurrences": [d.isoformat(timespec="seconds") for d in dates],
    }


@router.get("/api/recurring")
def list_recurring():
    """Every rhythm the app is keeping, with its rule in words.

    Mostly for looking under the bonnet: the page reads each task's own
    `recurrence`, which is the same data attached where it is used.
    """
    settings = db.get_settings()
    return {"series": [recurring.describe(s, settings)
                       for s in db.list_series(active_only=False)]}


@router.post("/api/recurring/run")
def run_recurring():
    """Run the scheduled sweep now, and hand back the page.

    The same call the background job makes once an hour, exposed because a
    sweep you cannot trigger is a sweep you cannot debug — and because it
    lets a real cron drive this instead, with `ADDERALL_RECUR_INTERVAL=0`.
    """
    result = scheduler.run_once()
    state = core.state()
    state["recurring"] = {"ran_at": result["ran_at"],
                          "created": [t["id"] for t in result["created"]]}
    return state
