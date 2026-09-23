"""Retry queue for outbound calls (to Claude, to ClickUp) that failed because
this machine itself has no route to the internet — as opposed to the browser
losing its connection to *this app*, which the front end handles entirely on
its own (see `api()` in app.js).

A route hands a failed call here instead of erroring out to the user only
when the failure is a `NetworkRetry` — raised by a handler for
`ai.AINetworkError` or `clickup.ClickUpNetworkError`, the two exceptions that
mean "the internet itself is unreachable" rather than "this request is bad".
Everything else (a missing API key, an invalid ClickUp token, a model
refusal) still fails immediately: retrying later cannot fix those, so
queuing them would just be a slower way to fail the same way.

Queued items are persisted (see db.queue_*) so a queued breakdown or sync
survives a restart, and retried on a plain timer — the same "sweep, don't
schedule" shape as scheduler.py and clickup.py, for the same reason: this
runs on a machine that sleeps.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from . import db

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60
MIN_INTERVAL_SEC = 15


class NetworkRetry(Exception):
    """A handler raises this to mean: still no route to the internet, try
    again on the next sweep. Anything else a handler raises fails the queued
    item for good — it is logged and dropped rather than retried forever."""


# kind -> callable(payload: dict). Registered by main.py at import time, once
# the route handlers whose work each one mirrors already exist.
_HANDLERS: dict[str, Callable[[dict], None]] = {}


def register(kind: str, handler: Callable[[dict], None]) -> None:
    _HANDLERS[kind] = handler


def enqueue(kind: str, payload: dict) -> dict:
    if kind not in _HANDLERS:
        raise ValueError(f"no queue handler registered for kind={kind!r}")
    row = db.queue_add(kind, payload)
    log.warning("queue: no route to the internet — queued %s (id=%s) to retry",
                kind, row["id"])
    return row


def interval_seconds() -> int:
    """How often to retry queued items. `ADDERALL_QUEUE_INTERVAL=0` turns
    retrying off — items stay queued until re-enabled or run by hand."""
    try:
        value = int(os.environ.get("ADDERALL_QUEUE_INTERVAL", DEFAULT_INTERVAL_SEC))
    except ValueError:
        return DEFAULT_INTERVAL_SEC
    if value <= 0:
        return 0
    return max(MIN_INTERVAL_SEC, value)


def _process_one(row: dict) -> None:
    handler = _HANDLERS.get(row["kind"])
    if handler is None:
        log.error("queue: no handler registered for kind=%s (id=%s); dropping",
                   row["kind"], row["id"])
        db.queue_remove(row["id"])
        return
    try:
        handler(row["payload"])
    except NetworkRetry as exc:
        log.warning("queue: %s (id=%s) still unreachable: %s",
                     row["kind"], row["id"], exc)
        db.queue_retry(row["id"], str(exc))
        return
    except Exception:
        log.exception("queue: %s (id=%s) failed for good; dropping",
                       row["kind"], row["id"])
        db.queue_remove(row["id"])
        return
    db.queue_remove(row["id"])
    log.info("queue: %s (id=%s) went through", row["kind"], row["id"])


def run_once() -> int:
    """One sweep, synchronously. Returns how many items it looked at."""
    rows = db.queue_pending()
    for row in rows:
        _process_one(row)
    return len(rows)


async def _loop(interval: int) -> None:
    while True:
        try:
            # AI/HTTP + SQLite work is blocking; keep it off the event loop.
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("queue: sweep failed; retrying next tick")
        await asyncio.sleep(interval)


def start(app) -> asyncio.Task | None:
    """Kick the retry sweep off on startup and keep it ticking."""
    interval = interval_seconds()
    if not interval:
        log.info("queue: retrying disabled (ADDERALL_QUEUE_INTERVAL=0)")
        return None
    log.info("queue: retrying every %ds", interval)
    task = asyncio.create_task(_loop(interval))
    app.state.queue_task = task
    return task


async def stop(app) -> None:
    task = getattr(app.state, "queue_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    app.state.queue_task = None
