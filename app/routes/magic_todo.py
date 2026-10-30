"""Magic ToDo: ⚡ Break down, the AI splitting one task into steps."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import ai, core, db, modules, queue
from . import estimates

router = APIRouter(dependencies=[Depends(modules.require("magic_todo"))])


class BreakdownRequest(BaseModel):
    granularity: int | None = Field(default=None, ge=1, le=5)



@router.post("/api/tasks/{task_id}/breakdown")
def breakdown_task(task_id: str, body: BreakdownRequest):
    task = core.require_task(task_id)
    settings = db.get_settings()
    granularity = body.granularity or settings["granularity"]
    parents = []
    cursor = task
    while cursor.get("parent_id"):
        cursor = db.get_task(cursor["parent_id"])
        if not cursor:
            break
        parents.insert(0, cursor["title"])
    try:
        steps = ai.breakdown(settings, task["title"], task["description"],
                             granularity, parents)
    except ai.AINetworkError:
        queue.enqueue("breakdown", {"task_id": task_id, "granularity": granularity})
        return core.queued_state(
            "breakdown",
            "No internet connection on the server — this breakdown is "
            "queued and will run automatically once it's back.",
        )
    except ai.AIUnavailable as exc:
        raise HTTPException(502, str(exc))
    new_ids = []
    for step in steps:
        sub = db.create_task({"title": step, "parent_id": task_id,
                              "project_id": task["project_id"]})
        new_ids.append(sub["id"])
    if new_ids:
        core.reveal(task_id)
    estimates.annotate_tasks(new_ids, want_scores=settings["ai_scoring"])
    return core.state()


def _process_breakdown(payload: dict) -> None:
    task_id = payload["task_id"]
    task = db.get_task(task_id)
    if task is None:
        return  # deleted before the queue got to it
    settings = db.get_settings()
    granularity = payload.get("granularity") or settings["granularity"]
    parents = []
    cursor = task
    while cursor.get("parent_id"):
        cursor = db.get_task(cursor["parent_id"])
        if not cursor:
            break
        parents.insert(0, cursor["title"])
    try:
        steps = ai.breakdown(settings, task["title"], task["description"],
                             granularity, parents)
    except ai.AINetworkError as exc:
        raise queue.NetworkRetry(str(exc)) from exc
    new_ids = []
    for step in steps:
        sub = db.create_task({"title": step, "parent_id": task_id,
                              "project_id": task["project_id"]})
        new_ids.append(sub["id"])
    if new_ids:
        core.reveal(task_id)
    estimates.annotate_tasks(new_ids, want_scores=settings["ai_scoring"])


queue.register("breakdown", _process_breakdown, module="magic_todo")
