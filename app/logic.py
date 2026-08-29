"""Deterministic scheduling, buffering, and prioritization.

Everything in this module is pure local computation: the time-tax buffer,
action-priority-matrix placement, urgency scoring, auto-deadlines with
backward scheduling, and the "what next" ordering. No AI calls here — that
keeps recomputation instant, consistent, and testable.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

BUFFER_MIN = 0.25
BUFFER_MAX = 0.50
DEFAULT_ESTIMATE_MIN = 30  # used for urgency math when a task has no estimate

QUADRANT_RANK = {"quick_win": 0, "major_project": 1, "fill_in": 2, "thankless": 3, None: 2}

# Default deadline horizons (days) per quadrant for standalone auto-deadlines:
# quick wins surface soon, major projects get lead time, thankless sink.
HORIZON_DAYS = {"quick_win": 1, "fill_in": 3, "major_project": 7, "thankless": 14, None: 3}

ACTIVE_STATUSES = {"todo", "in_progress"}


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


def urgency(deadline: datetime | None, buffered_min: int | None, now: datetime) -> float:
    """0.5 (no deadline / distant) → 10 (overdue or no slack).

    Rises as remaining time shrinks relative to the buffered estimate: a task
    whose remaining window is barely bigger than the work itself has no slack.
    """
    if deadline is None:
        return 0.5
    remaining_min = (deadline - now).total_seconds() / 60.0
    if remaining_min <= 0:
        return 10.0
    work = buffered_min if buffered_min and buffered_min > 0 else DEFAULT_ESTIMATE_MIN
    return round(min(10.0, max(0.5, 10.0 * work / remaining_min)), 1)


def compute(tasks: list[dict], settings: dict, ratios: list[float] | None = None,
            now: datetime | None = None) -> dict[str, dict]:
    """Compute all derived fields for a flat task list.

    Returns {task_id: {buffered_estimate, quadrant, urgency, deadline,
    deadline_source, sort_key, actionable}}.
    """
    now = now or datetime.now(timezone.utc)
    ratios = ratios or []
    buf = effective_buffer(settings, ratios)
    threshold = int(settings.get("matrix_threshold", 5))
    auto_deadlines = bool(settings.get("auto_deadlines", True))

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

    def resolve_deadline(task: dict, parent_deadline: datetime | None) -> tuple[datetime | None, str]:
        user_dl = parse_dt(task["deadline"])
        if user_dl:
            return user_dl, "user"
        if not auto_deadlines:
            return None, "none"
        if parent_deadline:
            # Backward scheduling: this child must finish early enough to leave
            # room for the buffered estimates of every later sibling.
            sibs = children.get(task["parent_id"], [])
            idx = next(i for i, s in enumerate(sibs) if s["id"] == task["id"])
            tail_min = sum(
                (derived[s["id"]]["buffered_estimate"] or DEFAULT_ESTIMATE_MIN)
                for s in sibs[idx + 1:]
                if s["status"] in ACTIVE_STATUSES
            )
            return parent_deadline - timedelta(minutes=tail_min), "auto"
        created = parse_dt(task["created_at"]) or now
        days = HORIZON_DAYS.get(derived[task["id"]]["quadrant"], 3)
        return created + timedelta(days=days), "auto"

    def walk(parent_id: str | None, parent_deadline: datetime | None) -> None:
        for task in children.get(parent_id, []):
            dl, source = resolve_deadline(task, parent_deadline)
            d = derived[task["id"]]
            d["deadline"] = dl.isoformat(timespec="seconds") if dl else None
            d["deadline_source"] = source
            if task["status"] in ACTIVE_STATUSES:
                d["urgency"] = urgency(dl, d["buffered_estimate"], now)
            else:
                d["urgency"] = 0.0
            walk(task["id"], dl)

    walk(None, None)

    for t in tasks:
        d = derived[t["id"]]
        open_children = [
            c for c in children.get(t["id"], []) if c["status"] in ACTIVE_STATUSES
        ]
        d["actionable"] = t["status"] in ACTIVE_STATUSES and not open_children
        d["sort_key"] = [
            -d["urgency"],
            QUADRANT_RANK.get(d["quadrant"], 2),
            t["order_index"],
            t["created_at"],
        ]
    return derived


def next_task(tasks: list[dict], derived: dict[str, dict]) -> dict | None:
    """The single task Taskmaster should surface next: highest urgency first,
    quick wins break ties, thankless work sinks."""
    candidates = [t for t in tasks if derived[t["id"]]["actionable"]]
    if not candidates:
        return None
    candidates.sort(key=lambda t: derived[t["id"]]["sort_key"])
    return candidates[0]
