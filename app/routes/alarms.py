"""Transition alarms as a server-sent event stream. See `events.py`."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from sse_starlette import EventSourceResponse

from .. import events, modules

router = APIRouter(dependencies=[Depends(modules.require("alarms"))])


@router.get("/api/events")
async def stream_events():
    """Transition alarms as they fire, as server-sent events (`event: alarm`,
    the event as JSON in `data`). The page listens here for its banner; so can
    anything else. See `events.py`."""
    inbox: asyncio.Queue = asyncio.Queue()
    events.subscribers.add(inbox)

    async def stream():
        try:
            while True:
                event = await inbox.get()
                yield {"event": event["type"], "data": json.dumps(event)}
        finally:
            events.subscribers.discard(inbox)

    return EventSourceResponse(stream(), ping=15)
