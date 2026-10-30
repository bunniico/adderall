"""ClickUp sync. See `clickup.py`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import clickup, core, db, modules, queue

router = APIRouter(dependencies=[Depends(modules.require("clickup"))])


# ---------- ClickUp sync ----------
# A one-way mirror of "tasks assigned to me" into a dedicated project — see
# clickup.py. This route runs the same pass the background loop does on a
# timer, exposed because a sync you cannot trigger on demand is a sync you
# cannot test right after pasting in a token.

@router.post("/api/clickup/sync")
def sync_clickup():
    settings = db.get_settings()
    try:
        result = clickup.sync(settings)
    except clickup.ClickUpNetworkError:
        queue.enqueue("clickup_sync", {})
        return core.queued_state(
            "clickup_sync",
            "No internet connection on the server — sync is queued and will "
            "run automatically once it's back.",
        )
    except clickup.ClickUpUnavailable as exc:
        raise HTTPException(502, str(exc))
    state = core.state()
    state["clickup"] = result
    return state


def _process_clickup_sync(payload: dict) -> None:
    settings = db.get_settings()
    try:
        clickup.sync(settings)
    except clickup.ClickUpNetworkError as exc:
        raise queue.NetworkRetry(str(exc)) from exc
    except clickup.ClickUpUnavailable:
        # The token was removed or is now invalid — nothing a retry can fix.
        pass


queue.register("clickup_sync", _process_clickup_sync, module="clickup")
