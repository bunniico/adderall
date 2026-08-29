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

    # Roll subtree totals up: a parent's real cost is the sum of everything
    # underneath it, and its real deadline is the furthest one inside it.
    for t in tasks:
        if t["parent_id"] not in by_id:
            _rollup(t, children, derived)

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
                                   d["rollup_remaining"], now)
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
