"""Braindump: the compiler that turns free text into a tree of tasks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import ai, core, db, modules, queue
from . import estimates

router = APIRouter(dependencies=[Depends(modules.require("braindump"))])


class CompileRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)



def _plant_compiled(items: list[dict], project_id: str) -> list[str]:
    """Store a compiled braindump tree as real parent/child tasks, depth
    first. Shared by the route and its queued retry."""
    new_ids: list[str] = []

    def plant(nodes: list[dict], parent_id: str | None) -> None:
        for item in nodes:
            task = db.create_task({
                "title": item["title"].strip(),
                "description": item.get("description", "").strip(),
                "parent_id": parent_id,
                "project_id": project_id,
            })
            new_ids.append(task["id"])
            plant(item.get("subtasks") or [], task["id"])

    plant(items, None)
    return new_ids


@router.post("/api/compile")
def compile_braindump(body: CompileRequest):
    settings = db.get_settings()
    project_id = core.active_project_id(db.list_projects() or [db.ensure_project()])
    try:
        items = ai.compile_braindump(settings, body.text)
    except ai.AINetworkError:
        queue.enqueue("compile", {"text": body.text, "project_id": project_id})
        return core.queued_state(
            "compile",
            "No internet connection on the server — this braindump is "
            "queued and will compile automatically once it's back.",
        )
    except ai.AIUnavailable as exc:
        raise HTTPException(502, str(exc))
    new_ids = _plant_compiled(items, project_id)
    estimates.annotate_tasks(new_ids, want_scores=settings["ai_scoring"],
                             want_start=settings["ai_start_times"])
    return core.state()


def _process_compile(payload: dict) -> None:
    project = db.get_project(payload["project_id"])
    if project is None:
        return  # the project was deleted before the queue got to it
    settings = db.get_settings()
    try:
        items = ai.compile_braindump(settings, payload["text"])
    except ai.AINetworkError as exc:
        raise queue.NetworkRetry(str(exc)) from exc
    new_ids = _plant_compiled(items, payload["project_id"])
    estimates.annotate_tasks(new_ids, want_scores=settings["ai_scoring"],
                             want_start=settings["ai_start_times"])


queue.register("compile", _process_compile, module="braindump")
