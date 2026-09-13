"""The jobs that run on a timer, and the rule for when the plan is remade.

## When the scheduler runs

It is worth saying out loud, because "I'm not sure when it runs or if it has
any routines at all" was a fair thing to say about it. Placement used to be
recomputed inside every request, on a day book built for that request and
thrown away with it, so the app was rearranging your week every time you
looked at it and never rearranging it when something actually changed.

**What is replanned.** Only work the app placed itself: a task with no
deadline of your own, which it gave a day and an hour to. A deadline you set,
a start time you set, and the shape of a tree are never touched.

**When.** The plan is remade on the first read after anything that could move
work about: any task written (created, edited, finished, dropped, moved,
deleted), any setting the plan is made of (the hours of your day, the cap, the
buffer, the timezone), the local day rolling over, and the sweep below
planting a copy of a repeating job. Nothing else. In particular, *reading*
never replans: two reads in a row give the same answer, which is the whole
point.

**How it is known.** `db.plan_rev` counts changes; the plan records the rev it
was made against. Equal means current. That is the entire mechanism.

**Blast radius.** Today onward. The past is never rewritten, so work that is
already overdue stays overdue rather than quietly rescheduling itself out of
the red.

## The recurrence sweep

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

from datetime import datetime, timezone

from . import db, logic, recurring

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
    """One pass, synchronously. The API route and the loop share this.

    The sweep, and then the day-rollover check: a plan made yesterday was made
    for a day that no longer exists, so the rev is bumped and the next read
    remakes it. Bumping rather than planning here keeps the placement in one
    place (the read path) instead of two.
    """
    result = recurring.sweep()
    result["rolled_over"] = _roll_over()
    return result


def _roll_over() -> bool:
    """Has the local day turned since the plan was made? Say so, once."""
    settings = db.get_settings()
    today = datetime.now(timezone.utc).astimezone(
        logic.resolve_tz(settings.get("timezone"))).date().isoformat()
    state = db.plan_state()
    if state["planned_day"] is None or state["planned_day"] == today:
        return False
    log.info("recurring: the day turned; the plan will be remade on next read")
    db.bump_plan_rev()
    return True


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
