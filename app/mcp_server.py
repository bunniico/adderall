"""The app as an MCP server, so an AI agent can read and work the task list.

Served over streamable HTTP at `/mcp` on the same port as the page. Each tool
is a thin call into the same route function the page uses, so an agent sees
exactly what the page would show and its changes land exactly as a click
would. Anything a tool changes comes back as the task tree it changed.

Only `localhost` may connect (the SDK's DNS-rebinding guard), because nothing
here asks who is calling.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from . import events

INSTRUCTIONS = """\
adderall is a single-user task manager for people with executive dysfunction.
Tasks live in projects (tabs) and nest into subtasks. The app derives each
task's deadline, priority and quadrant; `next_task` is the one thing to do now.
Times are ISO 8601. Transition alarms (stop / get ready / go before a deadline)
are listed by `recent_alarms`."""


async def _call(fn, *args):
    """Run a route function off the event loop, turning its HTTP and
    validation errors into messages an agent can read (the SDK hides the text
    of any other exception)."""
    try:
        return await asyncio.to_thread(fn, *args)
    except HTTPException as e:
        raise ToolError(e.detail) from None
    except ValidationError as e:
        raise ToolError(str(e)) from None


def build() -> MCPServer:
    from . import main  # the routes; imported here because main imports us

    server = MCPServer("adderall", instructions=INSTRUCTIONS)

    @server.tool()
    async def list_projects() -> list[dict]:
        """Every project (tab), with how many open tasks each holds."""
        state = await _call(main.get_state)
        return state["projects"]

    @server.tool()
    async def list_tasks(project_id: str | None = None) -> list[dict]:
        """The task tree of one project, derived deadline and priority
        included. Defaults to the project open in the app."""
        state = await _call(main._state, project_id)
        return state["tasks"]

    @server.tool()
    async def next_task() -> dict | None:
        """The one task to do next, or null when there is nothing to do."""
        return (await _call(main.get_next))["task"]

    @server.tool()
    async def add_task(title: str, description: str = "",
                       parent_id: str | None = None, deadline: str | None = None,
                       estimated_time: int | None = None) -> list[dict]:
        """Add a task to the open project, or under `parent_id` as a subtask.
        `deadline` is ISO 8601; `estimated_time` is minutes. A title such as
        "call mum tomorrow at 5pm" sets its own deadline."""
        return (await _call(lambda: main.create_task(main.TaskCreate(
            title=title, description=description, parent_id=parent_id,
            deadline=deadline, estimated_time=estimated_time))))["tasks"]

    @server.tool()
    async def update_task(task_id: str, changes: dict) -> list[dict]:
        """Edit a task. `changes` may hold title, description, deadline,
        clear_deadline, start_at, clear_start_at, estimated_time (minutes),
        impact and effort (0-10), status, flexibility (1-5), workday_only."""
        return (await _call(lambda: main.update_task(
            task_id, main.TaskUpdate(**changes))))["tasks"]

    @server.tool()
    async def start_task(task_id: str) -> list[dict]:
        """Mark a task in progress; its time starts counting."""
        return (await _call(main.start_task, task_id))["tasks"]

    @server.tool()
    async def complete_task(task_id: str, actual_time: int | None = None) -> list[dict]:
        """Mark a task done. `actual_time` is minutes spent; left out, it is
        measured from when the task was started."""
        return (await _call(lambda: main.complete_task(
            task_id, main.CompleteRequest(actual_time=actual_time))))["tasks"]

    @server.tool()
    async def delete_task(task_id: str) -> list[dict]:
        """Delete a task and its subtasks."""
        return (await _call(main.delete_task, task_id))["tasks"]

    @server.tool()
    async def compile_braindump(text: str) -> list[dict]:
        """Turn free text into a tree of tasks in the open project (uses AI)."""
        return (await _call(lambda: main.compile_braindump(
            main.CompileRequest(text=text))))["tasks"]

    @server.tool()
    async def list_habits() -> dict:
        """Routines, their streaks, and which days they were done."""
        return await _call(main.get_habits)

    @server.tool()
    async def check_habit(habit_id: str, done: bool = True,
                          day: str | None = None) -> dict:
        """Mark a habit done (or not) for `day` (YYYY-MM-DD, default today)."""
        return await _call(lambda: main.check_habit(
            habit_id, main.HabitCheck(done=done, day=day)))

    @server.tool()
    async def recent_alarms(limit: int = 20) -> list[dict]:
        """The latest transition alarms the app raised, newest last."""
        return list(events.recent)[-limit:]

    return server
