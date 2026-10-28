"""AI estimates: the estimate, impact/effort scores and start time the AI
fills in on a task, and the 🎲 Re-estimate that asks again."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from .. import ai, core, db, modules, queue

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(modules.require("ai_estimates"))])


def annotate_tasks(task_ids: list[str], want_scores: bool,
                   want_start: bool = False) -> None:
    """Best effort: fill missing estimates, scores and start times via one
    batched fast-tier call. Never blocks or fails the surrounding operation.

    Start times are only ever written onto top-level tasks. A step inside a
    task is scheduled from the slot its parent was given — that is what makes
    a plan one block on the calendar rather than a scatter — so a start time
    on one would be a number nothing reads.

    Does nothing while the AI estimates module is off.
    """
    settings = db.get_settings()
    if not modules.enabled("ai_estimates", settings):
        return
    targets = []
    for tid in task_ids:
        t = db.get_task(tid)
        if not t:
            continue
        wants_start = want_start and t["parent_id"] is None and t["start_at"] is None
        if (t["estimated_time"] is None or wants_start or
                (want_scores and (t["impact"] is None or t["effort"] is None))):
            targets.append(t)
    if not targets:
        return
    ask_start = want_start and any(t["parent_id"] is None for t in targets)
    try:
        _apply_annotations(settings, targets, want_scores, ask_start)
    except ai.AINetworkError:
        # The server itself has no route to the internet right now — queue it
        # rather than silently giving up; queue.py retries once it does.
        queue.enqueue("annotate_batch", {
            "task_ids": [t["id"] for t in targets],
            "want_scores": want_scores, "want_start": ask_start,
        })
    except ai.AIUnavailable as exc:
        log.warning("best-effort annotate skipped: %s", exc)


def _apply_annotations(settings: dict, targets: list[dict], want_scores: bool,
                       ask_start: bool) -> None:
    """The actual annotate call and applying its results — shared by the
    best-effort path above and the queued retry in `_process_annotate_batch`.
    """
    results = ai.annotate(settings, targets, want_scores=want_scores,
                          want_start=ask_start,
                          now_local=core.now_local(settings) if ask_start else "")
    now = datetime.now(timezone.utc)
    for t in targets:
        row = results.get(t["id"])
        if not row:
            continue
        fields = {}
        if t["estimated_time"] is None and row.get("minutes"):
            fields["estimated_time"] = int(row["minutes"])
        if want_scores and t["impact"] is None and row.get("impact") is not None:
            fields["impact"] = int(row["impact"])
        if want_scores and t["effort"] is None and row.get("effort") is not None:
            fields["effort"] = int(row["effort"])
        if (ask_start and t["parent_id"] is None and t["start_at"] is None
                and row.get("start_in_minutes") is not None):
            when = now + timedelta(minutes=int(row["start_in_minutes"]))
            fields["start_at"] = when.isoformat(timespec="seconds")
        if fields:
            db.update_task(t["id"], fields)


def _process_annotate_batch(payload: dict) -> None:
    """Queue handler for `annotate_batch`: re-reads the tasks fresh, since a
    field one of them was missing when queued may have been filled in by hand
    since — `_apply_annotations` only ever writes a field that is still
    None, so that is safe to check again here rather than trust the payload.
    """
    targets = [t for t in (db.get_task(tid) for tid in payload["task_ids"]) if t]
    if not targets:
        return
    settings = db.get_settings()
    try:
        _apply_annotations(settings, targets, payload["want_scores"],
                           payload["want_start"])
    except ai.AINetworkError as exc:
        raise queue.NetworkRetry(str(exc)) from exc


queue.register("annotate_batch", _process_annotate_batch, module="ai_estimates")


@router.post("/api/tasks/{task_id}/annotate")
def annotate_task(task_id: str):
    task = core.require_task(task_id)
    settings = db.get_settings()
    # Explicit re-annotation clears prior AI values so they refresh — the
    # start time included, since asking again is how you say "you had this
    # one wrong". Only for a top-level task: a step is scheduled inside its
    # parent's slot, so a start time on one would go unread.
    want_start = bool(settings["ai_start_times"]) and task["parent_id"] is None
    try:
        results = ai.annotate(settings, [task], want_scores=settings["ai_scoring"],
                              want_start=want_start,
                              now_local=core.now_local(settings) if want_start else "")
    except ai.AINetworkError:
        queue.enqueue("reannotate", {"task_id": task_id, "want_start": want_start})
        return core.queued_state(
            "reannotate",
            "No internet connection on the server — this re-estimate is "
            "queued and will run automatically once it's back.",
        )
    except ai.AIUnavailable as exc:
        raise HTTPException(502, str(exc))
    row = results.get(task_id)
    if row:
        fields = {"estimated_time": int(row["minutes"])}
        if settings["ai_scoring"]:
            if row.get("impact") is not None:
                fields["impact"] = int(row["impact"])
            if row.get("effort") is not None:
                fields["effort"] = int(row["effort"])
        if want_start and row.get("start_in_minutes") is not None:
            when = (datetime.now(timezone.utc)
                    + timedelta(minutes=int(row["start_in_minutes"])))
            fields["start_at"] = when.isoformat(timespec="seconds")
        db.update_task(task_id, fields)
    return core.state()


def _process_reannotate(payload: dict) -> None:
    task_id = payload["task_id"]
    task = db.get_task(task_id)
    if task is None:
        return  # deleted before the queue got to it
    settings = db.get_settings()
    want_start = payload.get("want_start", False)
    try:
        results = ai.annotate(settings, [task], want_scores=settings["ai_scoring"],
                              want_start=want_start,
                              now_local=core.now_local(settings) if want_start else "")
    except ai.AINetworkError as exc:
        raise queue.NetworkRetry(str(exc)) from exc
    row = results.get(task_id)
    if row:
        fields = {"estimated_time": int(row["minutes"])}
        if settings["ai_scoring"]:
            if row.get("impact") is not None:
                fields["impact"] = int(row["impact"])
            if row.get("effort") is not None:
                fields["effort"] = int(row["effort"])
        if want_start and row.get("start_in_minutes") is not None:
            when = (datetime.now(timezone.utc)
                    + timedelta(minutes=int(row["start_in_minutes"])))
            fields["start_at"] = when.isoformat(timespec="seconds")
        db.update_task(task_id, fields)


queue.register("reannotate", _process_reannotate, module="ai_estimates")
