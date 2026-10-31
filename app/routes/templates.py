"""Task templates: save a task and its steps under a name, reuse it later.

A template is the same picture a repeating job copies forward (see
`recurring.snapshot`): title, description, estimate, impact, effort and every
step underneath, without dates or status. Using one plants a fresh copy at the
top of the open project.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import core, db, recurring

router = APIRouter()


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _listing() -> dict:
    return {"templates": [{"id": t["id"], "name": t["name"]}
                          for t in db.list_templates()]}


@router.get("/api/templates")
def list_templates():
    return _listing()


@router.post("/api/tasks/{task_id}/template", status_code=201)
def save_template(task_id: str, body: TemplateCreate):
    core.require_task(task_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "A template needs a name")
    db.create_template(name, recurring.snapshot(task_id))
    return _listing()


@router.post("/api/templates/{template_id}/use", status_code=201)
def use_template(template_id: str):
    template = db.get_template(template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    project_id = core.active_project_id(db.list_projects() or [db.ensure_project()])
    recurring._plant(template["template"], project_id, None, None, None)
    return core.state()


@router.delete("/api/templates/{template_id}")
def delete_template(template_id: str):
    if not db.delete_template(template_id):
        raise HTTPException(404, "Template not found")
    return _listing()
