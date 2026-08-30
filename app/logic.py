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

# Priority score weights. Urgency carries the Eisenhower half (how close the
# deadline is relative to the work left), impact the importance half, and ease
# — a low effort score — breaks the ties in favour of quick wins. They sum to
# 1.0, so the score lands on a 0-100 scale you can read at a glance.
SCORE_WEIGHTS = {"urgency": 0.45, "impact": 0.35, "ease": 0.20}
NEUTRAL_SCORE = 5  # stand-in for an impact/effort the AI hasn't filled in yet

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


def priority_score(impact: int | None, effort: int | None, urgency_value: float) -> float:
    """0-100: one number for "what deserves the next hour".

    The week and month views put a whole day of tasks on screen at once, and
    a list in deadline order says nothing about which of them actually
    matters. This folds the three signals the app already keeps — deadline
    pressure (urgency), importance (impact) and cost (effort, counted as its
    inverse so cheap wins float) — into a single comparable number.

    Unscored tasks sit at neutral rather than at zero: a task nobody has
    rated yet is unknown, not worthless.
    """
    imp = NEUTRAL_SCORE if impact is None else max(0, min(10, impact))
    eff = NEUTRAL_SCORE if effort is None else max(0, min(10, effort))
    raw = (SCORE_WEIGHTS["urgency"] * max(0.0, min(10.0, urgency_value))
           + SCORE_WEIGHTS["impact"] * imp
           + SCORE_WEIGHTS["ease"] * (10 - eff))
    return round(raw * 10, 1)


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
            now: datetime | None = None) -> dict[str, dict]:
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
    """
    now = now or datetime.now(timezone.utc)
    ratios = ratios or []
    buf = effective_buffer(settings, ratios)
    threshold = int(settings.get("matrix_threshold", 5))
    auto_deadlines = bool(settings.get("auto_deadlines", True))
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

    def walk(parent_id: str | None, parent_deadline: datetime | None,
             prefix: tuple[int, ...]) -> None:
        for i, task in enumerate(children.get(parent_id, [])):
            dl, source = resolve_deadline(task, parent_deadline)
            d = derived[task["id"]]
            d["deadline"] = dl.isoformat(timespec="seconds") if dl else None
            d["deadline_source"] = source
            # Where this task sits in the hand-arranged tree, top down. Two
            # of these compare exactly the way the list reads: [0] < [0, 1]
            # (a task before its own subtasks) < [1].
            d["order_path"] = [*prefix, i]
            if task["status"] in ACTIVE_STATUSES:
                d["urgency"] = urgency(dl, d["buffered_estimate"], now)
            else:
                d["urgency"] = 0.0
            walk(task["id"], dl, (*prefix, i))

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
                                   d["rollup_remaining"], now)
        # Computed last, so containers score off the urgency of what they
        # actually still hold rather than off their own empty shell.
        d["score"] = priority_score(t["impact"], t["effort"], d["urgency"])
        d["length_min"] = block_length(d)
        d["raw_length_min"] = max(1, round(d["length_min"] / (1.0 + buf)))
        # Manual order means manual order: once you have arranged the list by
        # hand, the app stops second-guessing you and reads it top to bottom.
        d["sort_key"] = (manual_sort_key(t, d) if sort_field == "manual"
                         else smart_sort_key(t, d))
        d["list_sort_key"] = list_sort_key(t, d, sort_field, sort_desc)
    return derived


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
               new_deadline: datetime) -> dict[str, str]:
    """The deadlines to write when a past-due task is pushed to a new date.

    Returns {task_id: iso}. The task itself lands exactly on `new_deadline`;
    every task nested under it that carries a deadline *you* set slides by the
    same delta. That is what "the same length" means for anything bigger than
    one step: a plan spread over three days stays spread over three days
    instead of collapsing onto the new date. Auto-assigned deadlines are left
    alone — they are recomputed by backward scheduling from the new date on
    the very next read, which is the same shift by another route.

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
    moves = {task_id: new_deadline.isoformat(timespec="seconds")}

    current = parse_dt(derived.get(task_id, {}).get("deadline"))
    if current is None:
        return moves
    delta = new_deadline - current.replace(microsecond=0)

    children: dict[str | None, list[dict]] = {}
    for t in tasks:
        children.setdefault(t["parent_id"], []).append(t)
    stack = list(children.get(task_id, []))
    while stack:
        kid = stack.pop()
        stack.extend(children.get(kid["id"], []))
        own = parse_dt(kid["deadline"])
        if own is not None:
            moves[kid["id"]] = (own.replace(microsecond=0) + delta).isoformat(
                timespec="seconds")
    return moves
