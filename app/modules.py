"""The parts of the app that can be switched off, and the switch for each.

The task list, projects, scoring and the day planner are the app; they are
always on. Everything listed below is a module built on top of them, and each
one can be turned off in ⚙ Settings → Modules. Every module is on by default.

Off means off, not hidden:

- its API routes answer 404 (each module's router carries `require`);
- its background job, if it has one, skips its turn (the job checks
  `enabled` at the top of each pass, so turning a module back on needs no
  restart);
- anything it queued for retry waits in the queue until it is back on;
- the page hides every button, tab and setting that belongs to it.

Its data is never touched. Habits, repeat rules, XP and imported ClickUp tasks
all stay in the database, and turning the module back on picks them up where
they were.

The switches live in the `modules` setting, one boolean per module id.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from fastapi import HTTPException
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    group: str
    description: str


MODULES: tuple[Module, ...] = (
    Module("clickup", "ClickUp sync", "Integrations",
           "Mirror tasks assigned to you in ClickUp into their own tab."),
    Module("mcp", "MCP server", "Integrations",
           "Let AI agents read and work your list at /mcp."),
    Module("magic_todo", "Magic ToDo", "AI",
           "⚡ Break down: the AI splits a task into steps."),
    Module("braindump", "Braindump", "AI",
           "🧠 The compiler that turns a wall of text into tasks."),
    Module("ai_estimates", "AI estimates", "AI",
           "The AI fills in estimates, impact/effort scores and start times."),
    Module("calendar", "Calendar", "Views",
           "📅 Every dated task across every project, by day, week or month."),
    Module("overview", "Overview", "Views",
           "The tab that adds up every list."),
    Module("habits", "Habits", "Views",
           "The routines tab and its year of squares."),
    Module("help", "Help", "Views",
           "❓ One article per feature."),
    Module("recurring", "Repeating tasks", "Extras",
           "🔁 Tasks that come back on a schedule."),
    Module("focus", "Focus mode", "Extras",
           "▶ One task at a time with a visual timer."),
    Module("alarms", "Transition alarms", "Extras",
           "Stop / get ready / go cues, their event stream and webhooks."),
    Module("xp", "XP and levels", "Extras",
           "Finished work pays XP: the level bar, and confetti when you finish."),
    Module("sounds", "Sounds", "Extras",
           "Clicks, chimes and alarm tones."),
)

_BY_ID = {m.id: m for m in MODULES}

DEFAULTS = {m.id: True for m in MODULES}


def enabled(module_id: str, settings: dict | None = None) -> bool:
    """Is this module on? A module missing from the setting is on."""
    if settings is None:
        from . import db  # db imports this file for its defaults
        settings = db.get_settings()
    return bool((settings.get("modules") or {}).get(module_id, True))


def off_message(module_id: str) -> str:
    return (f"{_BY_ID[module_id].name} is turned off. "
            "Turn it back on in ⚙ Settings → Modules.")


def require(module_id: str):
    """A route dependency: 404 while the module is off."""
    def check() -> None:
        if not enabled(module_id):
            raise HTTPException(404, off_message(module_id))
    return check


def catalog() -> list[dict]:
    """Every module's id, name, group and description, for the settings page."""
    return [asdict(m) for m in MODULES]


class Gate:
    """Wrap a raw ASGI app (one that is not a FastAPI route, such as the MCP
    server) so it answers 404 while its module is off."""

    def __init__(self, module_id: str, app):
        self.module_id = module_id
        self.app = app

    async def __call__(self, scope, receive, send):
        if not await asyncio.to_thread(enabled, self.module_id):
            response = JSONResponse({"detail": off_message(self.module_id)}, 404)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
