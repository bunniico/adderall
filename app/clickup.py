"""ClickUp sync: pull tasks assigned to you into a dedicated project.

One-way and deliberately simple: every open task assigned to the
authenticated ClickUp user, across every workspace ("team") the token can
see, is mirrored into a `ClickUp` project as a normal task. A re-sync matches
existing rows by the ClickUp task id stored in `clickup_id` (see db.py) and
refreshes their title, description and due date — ClickUp stays the source
of truth for those three fields on an imported task, the same way the AI
owns a field only until you edit it yourself. Everything else (estimate,
impact/effort, status, subtasks, which project it lives in) is yours; sync
never touches it. Nothing is pushed back to ClickUp, and nothing already
imported is ever deleted or auto-completed here — a task that stops showing
up as "assigned and open" in ClickUp just stops being touched by future
syncs.

The background loop follows the same shape as `scheduler.py`: a sweep on a
timer rather than anything pinned to a clock, because this runs on a machine
that sleeps. It differs in one way — a fresh install has no ClickUp token
yet, and that is not an error worth logging every tick, so `run_once` skips
quietly until one is configured.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from . import db

log = logging.getLogger(__name__)

API_BASE = "https://api.clickup.com/api/v2"
TIMEOUT = 20.0
# A single-user app's "assigned to me" list is never going to run past this;
# it exists only so a misbehaving API can't turn a sync into an infinite loop.
MAX_PAGES = 50

PROJECT_NAME = "ClickUp"

DEFAULT_INTERVAL_SEC = 1800
MIN_INTERVAL_SEC = 60


class ClickUpUnavailable(Exception):
    """Raised when no API token is configured or the API call fails."""


def _token(settings: dict) -> str:
    token = (settings.get("clickup_api_token") or "").strip() \
        or os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token:
        raise ClickUpUnavailable(
            "ClickUp API token missing. Set it in Settings or via the "
            "CLICKUP_API_TOKEN environment variable."
        )
    return token


def _client(token: str) -> httpx.Client:
    return httpx.Client(base_url=API_BASE, headers={"Authorization": token},
                        timeout=TIMEOUT)


def _get(client: httpx.Client, path: str, **params) -> dict:
    try:
        resp = client.get(path, params=params)
    except httpx.RequestError as exc:
        raise ClickUpUnavailable(
            "Could not reach the ClickUp API (network error)."
        ) from exc
    if resp.status_code == 401:
        raise ClickUpUnavailable(
            "ClickUp API token missing or invalid. Set it in Settings or via "
            "the CLICKUP_API_TOKEN environment variable."
        )
    if resp.status_code >= 400:
        raise ClickUpUnavailable(
            f"ClickUp API error ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json()


def _normalize(task: dict) -> dict:
    """A raw ClickUp task, trimmed to the fields the sync actually stores."""
    deadline = None
    due = task.get("due_date")
    if due:
        try:
            deadline = datetime.fromtimestamp(
                int(due) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            deadline = None
    description = (task.get("text_content") or task.get("description") or "").strip()
    url = (task.get("url") or "").strip()
    if url:
        description = f"{description}\n\n{url}" if description else url
    return {
        "id": task["id"],
        "title": (task.get("name") or "Untitled ClickUp task").strip()[:500],
        "description": description,
        "deadline": deadline,
    }


def fetch_assigned_tasks(token: str) -> list[dict]:
    """Every open task assigned to the token's user, across every workspace.

    ClickUp has no single "every workspace" endpoint for this, so it is one
    call to find the user, one to list their workspaces ("teams"), and then
    one filtered, paginated call per workspace. "Filtered team tasks"
    excludes closed tasks by default, which is exactly "still open" — the
    same thing a re-sync needs on every later pass, not just the first.
    """
    with _client(token) as client:
        me = _get(client, "/user")["user"]
        teams = _get(client, "/team").get("teams") or []
        seen: set[str] = set()
        tasks: list[dict] = []
        for team in teams:
            for page in range(MAX_PAGES):
                data = _get(client, f"/team/{team['id']}/task",
                           **{"assignees[]": me["id"], "page": page})
                batch = data.get("tasks") or []
                for raw in batch:
                    if raw["id"] not in seen:
                        seen.add(raw["id"])
                        tasks.append(_normalize(raw))
                if len(batch) < 100:
                    break
    return tasks


def _ensure_project() -> dict:
    """The `ClickUp` project tab, created the first time a sync needs it."""
    for project in db.list_projects():
        if project["name"] == PROJECT_NAME:
            return project
    return db.create_project(PROJECT_NAME)


def sync(settings: dict | None = None) -> dict:
    """Pull assigned ClickUp tasks in. Creates or updates by `clickup_id`."""
    settings = settings or db.get_settings()
    token = _token(settings)
    remote = fetch_assigned_tasks(token)
    project = _ensure_project()

    created: list[str] = []
    updated: list[str] = []
    for rt in remote:
        local = db.get_task_by_clickup_id(rt["id"])
        if local is None:
            task = db.create_task({
                "title": rt["title"], "description": rt["description"],
                "deadline": rt["deadline"], "clickup_id": rt["id"],
                "project_id": project["id"],
            })
            created.append(task["id"])
        else:
            # Project membership is left alone on update: once imported, a
            # task is yours to move — sync must not drag it back to the
            # ClickUp tab because you filed it somewhere else.
            changed = {k: rt[k] for k in ("title", "description", "deadline")
                      if local.get(k) != rt[k]}
            if changed:
                db.update_task(local["id"], changed)
                updated.append(local["id"])

    db.update_settings({"clickup_last_sync_at": db.now_iso()})
    return {"project_id": project["id"], "fetched": len(remote),
            "created": created, "updated": updated}


def run_once() -> dict | None:
    """One sync, synchronously — the API route and the loop share this.

    Returns None without contacting ClickUp when no token is configured, so
    the background loop stays quiet on a fresh install instead of logging a
    failure every tick.
    """
    settings = db.get_settings()
    try:
        _token(settings)
    except ClickUpUnavailable:
        return None
    return sync(settings)


def interval_seconds() -> int:
    """How often to sync. `ADDERALL_CLICKUP_INTERVAL=0` turns the job off."""
    try:
        value = int(os.environ.get("ADDERALL_CLICKUP_INTERVAL", DEFAULT_INTERVAL_SEC))
    except ValueError:
        return DEFAULT_INTERVAL_SEC
    if value <= 0:
        return 0
    return max(MIN_INTERVAL_SEC, value)


async def _loop(interval: int) -> None:
    while True:
        try:
            # Blocking HTTP + SQLite work; keep it off the event loop.
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed sync is not a reason to stop syncing: the next pass
            # tries again against exactly the same state.
            log.exception("clickup: sync failed; retrying next tick")
        await asyncio.sleep(interval)


def start(app) -> asyncio.Task | None:
    """Kick the sync off on startup and keep it ticking."""
    interval = interval_seconds()
    if not interval:
        log.info("clickup: scheduled sync disabled (ADDERALL_CLICKUP_INTERVAL=0)")
        return None
    log.info("clickup: syncing every %ds", interval)
    task = asyncio.create_task(_loop(interval))
    app.state.clickup_task = task
    return task


async def stop(app) -> None:
    task = getattr(app.state, "clickup_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    app.state.clickup_task = None
