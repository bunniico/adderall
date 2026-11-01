"""💬 Ask: a chat that knows how the app works and can work it for you.

Answers come from the Help articles in `app/help/`, which already explain
every feature and why it behaves the way it does; they go in the system
prompt, cache-marked, so a conversation pays for them once. Actions go through
the MCP server's own tools (see `mcp_server.py`), called in-process: the chat
can do exactly what an outside agent connected to /mcp can do, no more, and
every change lands the way a click would.

The model only acts when asked. A question gets an answer; "add milk to my
list" gets a tool call. The conversation itself lives in the page and is sent
whole each time, so nothing here is stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import anthropic

from . import ai, db, logic

log = logging.getLogger(__name__)

HELP_DIR = os.path.join(os.path.dirname(__file__), "help")

# How many model round trips one message may take. Enough for "find the task,
# then change it, then check", and a hard stop on a loop that never settles.
MAX_STEPS = 8
MAX_TOKENS = 4096
# A tool result longer than this is cut, and says so. A big project's task
# tree is mostly derived fields the model does not need all of to answer.
MAX_RESULT_CHARS = 30_000

# Tools that only read. Anything else succeeding means the page should redraw.
READ_ONLY = {"list_projects", "list_tasks", "next_task", "list_habits",
             "recent_alarms"}

RULES = """\
You are the assistant built into adderall, a single-user task app for people
with executive dysfunction. You answer questions about how the app works, and
you can act on the user's tasks with the tools provided.

- Answer from the help articles below. If they do not cover something, say so
  rather than guessing.
- Only use a tool that changes anything (add, update, start, complete, delete,
  compile a braindump, check a habit) when the user has asked for that change.
  For a question, read with the list tools if you need to, then answer.
- Before deleting anything, or changing more than a few tasks at once, say what
  you are about to do and ask the user to confirm, unless they already did.
- Keep replies short and plain. After an action, say in one line what changed.
"""


class ChatUnavailable(Exception):
    """The chat could not answer, with a message the page can show."""


def _help_text() -> str:
    parts = []
    for name in sorted(os.listdir(HELP_DIR)):
        if name.endswith(".md"):
            with open(os.path.join(HELP_DIR, name), encoding="utf-8") as f:
                parts.append(f.read().strip())
    return "\n\n---\n\n".join(parts)


def _system(instructions: str) -> list[dict]:
    """Rules, how the tools see the app, and every help article. Identical on
    every call, so it is the cached prefix."""
    text = (f"{RULES}\n# How the app describes itself to tools\n\n{instructions}"
            f"\n\n# Help articles\n\n{_help_text()}")
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _tools(server) -> list[dict]:
    return [{"name": t.name, "description": t.description or "",
             "input_schema": t.input_schema}
            for t in asyncio.run(server.list_tools())]


def _run_tool(server, name: str, arguments: dict) -> tuple[str, bool]:
    """(result text, ok). A tool's own error comes back as text the model can
    read and explain, never as an exception out of the chat."""
    try:
        result = asyncio.run(server.call_tool(name, arguments or {}))
    except Exception as exc:  # ToolError, or bad arguments
        return str(exc), False
    if getattr(result, "is_error", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        return text or "The tool failed.", False
    text = "\n".join(getattr(c, "text", "") for c in result.content) or "null"
    if len(text) > MAX_RESULT_CHARS:
        text = (text[:MAX_RESULT_CHARS]
                + f"\n… [cut: {len(text) - MAX_RESULT_CHARS} more characters]")
    return text, True


def chat(settings: dict, history: list[dict], server) -> dict:
    """One reply to the conversation in `history` ([{role, content}], the last
    one the user's). Returns {reply, actions, changed}.

    Routed like every other call (see `ai._call`): the balanced tier, pulled
    down by the daily budget, every response logged and costed.
    """
    served = logic.throttled_tier("balanced", ai.throttle_stage(settings))
    model = settings.get("models", {}).get(served) or "claude-haiku-4-5"
    system = _system(server.instructions or "")
    tools = _tools(server)
    messages: list = [{"role": m["role"], "content": m["content"]} for m in history]
    actions: list[dict] = []
    client = ai._client(settings)

    for _ in range(MAX_STEPS):
        log.info("chat → tier=balanced%s model=%s turns=%d",
                 f" (throttled to {served})" if served != "balanced" else "",
                 model, len(messages))
        try:
            response = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=system,
                tools=tools, messages=messages)
        except anthropic.AuthenticationError as exc:
            raise ChatUnavailable("Anthropic API key missing or invalid. Set it "
                                  "in Settings.") from exc
        except anthropic.APIConnectionError as exc:
            raise ChatUnavailable("Could not reach the Claude API (network "
                                  "error).") from exc
        except anthropic.APIStatusError as exc:
            raise ChatUnavailable(f"Claude API error ({exc.status_code}): "
                                  f"{exc.message}") from exc
        except TypeError as exc:
            # The SDK raises this when no credentials resolve at all.
            raise ChatUnavailable("No Anthropic API key set. Add one in "
                                  "Settings.") from exc
        ai._log_response(response)
        db.record_spend(served, model, ai.usage_cost(model, response.usage))

        if response.stop_reason == "refusal":
            return {"reply": "Sorry, I can't help with that one.",
                    "actions": actions, "changed": _changed(actions)}
        if response.stop_reason != "tool_use":
            reply = "\n\n".join(b.text for b in response.content
                                if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                reply += "\n\n(That reply ran out of room and was cut short.)"
            return {"reply": reply or "Done.", "actions": actions,
                    "changed": _changed(actions)}

        # Every block goes back as it came, thinking included, and every tool
        # result goes back in one message: the model may call several at once.
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            text, ok = _run_tool(server, block.name, block.input)
            log.info("chat tool %s(%s) → %s", block.name,
                     json.dumps(block.input)[:200], "ok" if ok else "error")
            actions.append({"tool": block.name, "ok": ok})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": text, **({} if ok else {"is_error": True})})
        messages.append({"role": "user", "content": results})

    return {"reply": "That took more steps than I'm allowed in one go. "
                     "Here's where I got to; ask me to carry on if you like.",
            "actions": actions, "changed": _changed(actions)}


def _changed(actions: list[dict]) -> bool:
    return any(a["ok"] and a["tool"] not in READ_ONLY for a in actions)
