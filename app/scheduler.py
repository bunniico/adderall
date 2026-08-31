"""The scheduled job that keeps recurring tasks appearing.

It is deliberately a sweep on a timer rather than an alarm clock set for
midnight. This app runs on one machine — a laptop that sleeps, a Pi that gets
unplugged, a container that is restarted mid-deploy — and a job which only
works if the process happens to be awake at 00:00 is a job that silently
stops working. So the sweep runs once at startup and then on a plain interval,
and `recurring.sweep` is written to be idempotent: whether it last ran an hour
ago or three weeks ago, it brings every rhythm to exactly the same place.

The interval is hourly by default, which is a *finer* grain than the daily
check the feature needs. That is the point: a daily boundary crossed while the
machine was closed is picked up within the hour of it opening again, and an
extra twenty-three no-op queries a day against a local SQLite file cost
nothing worth measuring.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import recurring

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 3600
MIN_INTERVAL_SEC = 30


def interval_seconds() -> int:
    """How often to sweep. `ADDERALL_RECUR_INTERVAL=0` turns the job off —
    the tests run it by hand, and so can a real cron if you'd rather."""
    try:
        value = int(os.environ.get("ADDERALL_RECUR_INTERVAL", DEFAULT_INTERVAL_SEC))
    except ValueError:
        return DEFAULT_INTERVAL_SEC
    if value <= 0:
        return 0
    return max(MIN_INTERVAL_SEC, value)


def run_once() -> dict:
    """One sweep, synchronously. The API route and the loop share this."""
    return recurring.sweep()


async def _loop(interval: int) -> None:
    while True:
        try:
            # SQLite work is blocking; keep it off the event loop so a sweep
            # never delays a request the user is waiting on.
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed sweep is not a reason to stop sweeping: the next pass
            # sees exactly the same work, because the pass is idempotent.
            log.exception("recurring: sweep failed; retrying next tick")
        await asyncio.sleep(interval)


def start(app) -> asyncio.Task | None:
    """Kick the sweep off on startup and keep it ticking."""
    interval = interval_seconds()
    if not interval:
        log.info("recurring: scheduled sweep disabled (ADDERALL_RECUR_INTERVAL=0)")
        return None
    log.info("recurring: sweeping every %ds", interval)
    task = asyncio.create_task(_loop(interval))
    app.state.recurring_task = task
    return task


async def stop(app) -> None:
    task = getattr(app.state, "recurring_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    app.state.recurring_task = None
