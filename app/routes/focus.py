"""Focus mode: the walk Taskmaster takes through a task tree."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import core, db, logic, modules

router = APIRouter(dependencies=[Depends(modules.require("focus"))])


@router.get("/api/focus")
def get_focus(root: str | None = None):
    """The ordered walk Taskmaster follows through a task tree.

    `root` scopes the session to one task's subtree; without it the most
    urgent tree in the open tab is chosen — or in any tab at all, when the
    open tab is the compiled All list. The queue is depth-first — subtasks
    before the task that contains them — so a session drills down to the
    smallest first step and works its way back up.

    A named root keeps the session in *its* project, whichever tab is open:
    ducking out to another project to jot something down must not hijack a
    running timer onto a different task.
    """
    projects = db.list_projects() or [db.ensure_project()]
    settings = db.get_settings()
    # A named root brings its own project, so the lens only decides anything
    # when the session is being chosen rather than resumed.
    lens = core.all_tasks_view(settings, projects) and not root
    project_id = (core.require_task(root)["project_id"] if root
                  else core.active_project_id(projects))
    tasks = db.list_tasks(None if lens else project_id)
    derived = logic.compute(tasks, core.list_settings(settings, lens),
                            db.completion_ratios())
    root_id = root or logic.focus_root_id(tasks, derived)
    if not root_id:
        return {"root_id": None, "root_title": None, "project_id": project_id,
                "queue": []}
    by_id = {t["id"]: t for t in tasks}
    if root_id not in by_id:
        raise HTTPException(404, "Task not found")
    if lens:
        # The session belongs to the project the chosen tree lives in, not to
        # whichever tab the All list was opened from.
        project_id = by_id[root_id]["project_id"]
    queue = [
        {**t, **derived[t["id"]], "path": logic.ancestor_titles(tasks, t)}
        for t in logic.focus_queue(tasks, root_id)
    ]
    return {
        "root_id": root_id,
        "root_title": by_id[root_id]["title"],
        "project_id": project_id,
        "queue": queue,
    }
