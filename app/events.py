"""Transition alarms, raised on the server and sent to whoever is listening.

The stop / get ready / go cues used to be worked out in the browser, which
meant they only existed while a tab was open, and nothing outside the page
could hear them. Now the server sweeps the deadlines on a timer and publishes
each cue once, as an event, to three kinds of listener:

- the page, over `GET /api/events` (server-sent events), which plays the sound
  and shows the banner exactly as before;
- every URL in the `webhooks` setting, POSTed as it fires (a Discord webhook
  URL gets a Discord-shaped message; anything else gets the event as JSON);
- anything that asks later, through the short history in `recent`, which is
  what the MCP server's `recent_alarms` tool reads.

The firing rule is the one the page used: a cue fires if its moment came up to
two minutes ago, and a cue further in the past than that is dropped silently,
so starting the app never replays a morning's worth of alarms. A cue is
remembered by task, stage and deadline, so moving a deadline arms its cues
again.

A task's planned slot (the first block the scheduler booked for it) gets the
same three cues, measured from when the slot starts rather than the deadline,
with `"kind": "start"` on the event (deadline cues carry `"kind": "deadline"`).
One wrinkle: once a slot's start goes by with the task still not started, the
planner re-books it from the next minute, over and over. Cues are therefore
timed off the start as it was planned while it was still ahead, so a slot
sliding forward is still one slot (its cues fire once), while moving it to a
genuinely later time arms them again. A task already in progress gets no start
cues: it has started.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx

from . import db, logic

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 30
WINDOW = timedelta(minutes=2)
WEBHOOK_TIMEOUT_SEC = 10

STAGES = (
    ("stop", "stop_lead", "⏸ Stop what you're doing — “{title}” is coming up"),
    ("ready", "ready_lead", "🧦 Get ready: “{title}”"),
    ("go", "go_lead", "🚀 Time for “{title}” — go now"),
)

START_STAGES = (
    ("stop", "stop_lead", "⏸ Stop what you're doing — “{title}” starts soon"),
    ("ready", "ready_lead", "🧦 Get ready: “{title}” is about to start"),
    ("go", "go_lead", "🚀 Time to start “{title}”"),
)
# The planner never books a slot sooner than this far ahead, so a first block
# starting any nearer than this is one that has slid, not a new plan.
SLIDE = timedelta(minutes=2)

subscribers: set[asyncio.Queue] = set()
recent: deque[dict] = deque(maxlen=50)
_fired: dict[str, datetime] = {}  # cue key -> when it was due
_planned_start: dict[str, datetime] = {}  # task id -> its slot's start, as planned


def interval_seconds() -> int:
    """How often to check. `ADDERALL_ALARM_INTERVAL=0` turns the loop off —
    the tests call `check` by hand."""
    try:
        value = int(os.environ.get("ADDERALL_ALARM_INTERVAL", DEFAULT_INTERVAL_SEC))
    except ValueError:
        return DEFAULT_INTERVAL_SEC
    return max(0, value)


def due_alarms(alarm_tasks: list[dict], settings: dict, now: datetime) -> list[dict]:
    """The cues that should fire now, each returned once over repeated calls."""
    alarms = settings.get("alarms") or {}
    if not alarms.get("enabled"):
        return []
    for key in [k for k, at in _fired.items() if now - at >= WINDOW]:
        del _fired[key]  # past the window; it can never fire again anyway
    out = []
    _track_planned_starts(alarm_tasks, now)
    for task in alarm_tasks:
        start = _planned_start.get(task["id"])
        if start is not None and task.get("status") != "in_progress":
            out += _cues(task, "start", start, START_STAGES, alarms, now)
        deadline = logic.parse_dt(task.get("deadline"))
        if deadline is not None:
            out += _cues(task, "deadline", deadline, STAGES, alarms, now)
    return out


def _track_planned_starts(alarm_tasks: list[dict], now: datetime) -> None:
    """Record each task's next slot start while it is still ahead of the
    planner's floor; a start inside it is a slot sliding, so keep the old one."""
    live = {t["id"] for t in alarm_tasks}
    for task_id in [k for k in _planned_start if k not in live]:
        del _planned_start[task_id]
    for task in alarm_tasks:
        blocks = task.get("blocks") or []
        start = logic.parse_dt(blocks[0][0]) if blocks else None
        if start is not None and start > now + SLIDE:
            _planned_start[task["id"]] = start


def _cues(task: dict, kind: str, at: datetime, stages, alarms: dict, now: datetime) -> list[dict]:
    out = []
    for stage, lead_key, text in stages:
        fire_at = at - timedelta(minutes=alarms.get(lead_key) or 0)
        if not (fire_at <= now < fire_at + WINDOW):
            continue
        key = f"{task['id']}:{kind}:{stage}:{at.isoformat()}"
        if key in _fired:
            continue
        _fired[key] = fire_at
        out.append({
            "type": "alarm",
            "kind": kind,
            "stage": stage,
            "text": text.format(title=task["title"]),
            "task": {**{k: task.get(k) for k in
                        ("id", "title", "deadline", "project_id", "project_name")},
                     "start": at.isoformat(timespec="seconds") if kind == "start" else None},
            "at": now.isoformat(timespec="seconds"),
        })
    return out


async def publish(event: dict, webhooks: list[str]) -> None:
    """Hand one event to the page, the history, and every webhook."""
    recent.append(event)
    for queue in list(subscribers):
        queue.put_nowait(event)
    urls = [u.strip() for u in webhooks if u.strip()]
    if urls:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SEC) as client:
            await asyncio.gather(*(_post(client, url, event) for url in urls))


async def _post(client: httpx.AsyncClient, url: str, event: dict) -> None:
    body = {"content": event["text"]} if _is_discord(url) else event
    try:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
    except Exception as e:
        # One unreachable webhook must not keep the cue from the others.
        log.warning("events: webhook %s failed: %s", _redact(url), e)


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks/" in url or "discordapp.com/api/webhooks/" in url


def _redact(url: str) -> str:
    """A webhook URL is its own password; log where it points, not the token."""
    return url.split("?")[0].rsplit("/", 1)[0] + "/…"


async def check(alarm_tasks_fn) -> list[dict]:
    """One pass: find what is due and publish it."""
    alarm_tasks, settings = await asyncio.to_thread(
        lambda: (alarm_tasks_fn(), db.get_settings()))
    fired = due_alarms(alarm_tasks, settings, datetime.now(timezone.utc))
    for event in fired:
        await publish(event, settings.get("webhooks") or [])
    return fired


async def _loop(interval: int, alarm_tasks_fn) -> None:
    while True:
        try:
            await check(alarm_tasks_fn)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("events: alarm check failed; retrying next tick")
        await asyncio.sleep(interval)


def start(app, alarm_tasks_fn) -> None:
    interval = interval_seconds()
    if not interval:
        log.info("events: alarm loop disabled (ADDERALL_ALARM_INTERVAL=0)")
        return
    app.state.alarm_task = asyncio.create_task(_loop(interval, alarm_tasks_fn))


async def stop(app) -> None:
    task = getattr(app.state, "alarm_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    app.state.alarm_task = None
