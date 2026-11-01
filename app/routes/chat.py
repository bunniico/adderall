"""💬 Ask: the in-app chat. See `chat.py`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import chat, core, db, modules

router = APIRouter(dependencies=[Depends(modules.require("ai_chat"))])


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=60)


@router.post("/api/chat")
def post_chat(body: ChatRequest):
    """One reply to the conversation the page sends. When the reply changed
    anything, the fresh page state rides along so the list redraws at once."""
    if body.messages[-1].role != "user":
        raise HTTPException(400, "The last message must be the user's")
    from ..main import mcp  # main imports this module
    try:
        result = chat.chat(db.get_settings(),
                           [m.model_dump() for m in body.messages], mcp)
    except chat.ChatUnavailable as exc:
        raise HTTPException(502, str(exc))
    if result["changed"]:
        result["state"] = core.state()
    return result
