"""Claude API broker.

The AI handles only fuzzy language judgments and always returns minimal
structured JSON; the app does all math and scheduling locally. Routing per
the design doc:

  - annotate (time estimate + impact/effort scores) → fast tier (Haiku),
    batched: many tasks in one call.
  - breakdown (Magic ToDo)                          → balanced tier (Sonnet),
    interactive, low effort for speed.
  - compile (braindump → task list)                 → deep tier (Opus),
    one batched call with adaptive thinking.
"""

from __future__ import annotations

import json
import logging
import os

import anthropic

LOG = logging.getLogger(__name__)

SYSTEM = (
    "You are the planning engine inside a task app for people with executive "
    "dysfunction (ADHD, autism, AuDHD). Be concrete and literal. Subtasks must "
    "be single physical or digital actions that can be started immediately, "
    "phrased as imperatives ('Put detergent in the machine'), never vague "
    "('Sort out laundry situation'). Time estimates are realistic working "
    "minutes for a distractible adult, not best-case minutes. Impact and "
    "effort are integers 0-10: impact = how much completing this improves the "
    "person's life or unblocks other work; effort = energy, friction, and "
    "executive load, not just duration. Return only the requested JSON."
)

GRANULARITY_HINTS = {
    1: "2-3 broad subtasks",
    2: "3-5 subtasks",
    3: "4-7 concrete subtasks",
    4: "6-10 small subtasks, each under 30 minutes",
    5: "8-14 tiny subtasks, each a single trivially startable action of a few minutes",
}


# Structured outputs support only a subset of JSON Schema: numeric bounds
# (minimum/maximum) and array-length constraints are rejected with a 400.
# Schemas below therefore carry types only, and every bound is enforced
# locally after parsing — which is where the app does its validation anyway.
MAX_STEPS = 16       # cap on subtasks from one breakdown
MAX_COMPILED = 60    # cap on tasks from one braindump, subtasks included
MAX_MINUTES = 60 * 24 * 30  # a month of minutes; anything larger is nonsense

BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["steps"],
    "additionalProperties": False,
}

COMPILE_DEPTH = 3    # levels of nesting a braindump may produce


def _compile_node_schema(depth: int) -> dict:
    """One task in a compiled braindump, nestable `depth` levels deep.

    The nesting is spelled out level by level rather than expressed as a
    recursive $ref: structured outputs have no way to bound recursion, and
    an unbounded tree invites the model to keep splitting hairs. A leaf
    level simply has no `subtasks` property, which is also what stops the
    model from nesting past the depth the app is willing to store.
    """
    props: dict = {
        "title": {"type": "string"},
        "description": {"type": "string"},
    }
    required = ["title", "description"]
    if depth > 1:
        props["subtasks"] = {
            "type": "array",
            "items": _compile_node_schema(depth - 1),
        }
        # Required, so a task with nothing under it says so with an empty
        # array instead of leaving the app to guess at a missing key.
        required.append("subtasks")
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


COMPILE_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": _compile_node_schema(COMPILE_DEPTH),
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}


def annotate_schema(want_scores: bool = True) -> dict:
    props: dict = {"id": {"type": "string"}, "minutes": {"type": "integer"}}
    required = ["id", "minutes"]
    if want_scores:
        props["impact"] = {"type": "integer"}
        props["effort"] = {"type": "integer"}
        required += ["impact", "effort"]
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }


def _clamp(value, low: int, high: int) -> int:
    """Coerce a model-supplied number into range; fall back to the low bound."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low


# How much of a prompt or completion goes into a single log record. A
# braindump can run to thousands of words, and dumping all of it buries
# everything else in `docker logs`; set ADDERALL_AI_LOG_CHARS=0 for no cap.
DEFAULT_AI_LOG_CHARS = 4000


def _log_chars() -> int:
    raw = os.environ.get("ADDERALL_AI_LOG_CHARS", "").strip()
    if not raw:
        return DEFAULT_AI_LOG_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_AI_LOG_CHARS


def _truncate(text: str) -> str:
    limit = _log_chars()
    if limit and len(text) > limit:
        return f"{text[:limit]}… [{len(text) - limit} more characters]"
    return text


def _log_text(label: str, text: str) -> None:
    """Log one labelled block of model text, newlines and all.

    These lines are read by a human tailing `docker logs`, so a braindump
    squeezed onto a single line would be worse than useless.
    """
    LOG.info("%s:\n%s", label, _truncate(text))


def _log_response(response) -> None:
    """Log what came back: the usage summary, any thinking, and the output."""
    usage = getattr(response, "usage", None)
    LOG.info(
        "AI response ← model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
        getattr(response, "model", "?"),
        getattr(response, "stop_reason", None),
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "thinking":
            # Only the compile call thinks, and only because it asks for a
            # summary: adaptive thinking with the default display returns
            # these blocks with empty text.
            thought = getattr(block, "thinking", "")
            if thought:
                _log_text("AI thinking", thought)
        elif kind == "redacted_thinking":
            LOG.info("AI thinking: (redacted by the API)")
        elif kind == "text":
            _log_text("AI output", block.text)


class AIUnavailable(Exception):
    """Raised when no API key is configured or the API call fails."""


def _client(settings: dict) -> anthropic.Anthropic:
    key = (settings.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY")
    # Identity-linked API keys must name the workspace each request acts in;
    # the SDK only sets this header automatically for federated credential
    # providers, so a plain api_key= client needs it passed explicitly.
    workspace = ((settings.get("workspace_id") or "").strip()
                 or os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip())
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    if key:
        return anthropic.Anthropic(api_key=key, default_headers=headers)
    # Fall back to the SDK's own resolution (auth token, ant profile, ...).
    return anthropic.Anthropic(default_headers=headers)


def _request(settings: dict, model: str, prompt: str, schema: dict,
             max_tokens: int, effort: str | None, thinking: bool):
    """One Messages API call, with API errors mapped onto AIUnavailable."""
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM,
        "cache_control": {"type": "ephemeral"},
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    if effort:
        kwargs["output_config"]["effort"] = effort
    if thinking:
        # `summarized` rather than the default `omitted`: the reasoning is
        # thought and billed either way, and the summary is what makes it
        # visible in the logs below.
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
    try:
        return _client(settings).messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise AIUnavailable(
            "Anthropic API key missing or invalid. Set it in Settings or via "
            "the ANTHROPIC_API_KEY environment variable."
        ) from exc
    except anthropic.APIStatusError as exc:
        if "anthropic-workspace-id" in str(exc.message):
            raise AIUnavailable(
                "Your API key is identity-linked, so it needs a workspace ID. "
                "Add it in Settings → Workspace ID (find it in the Claude "
                "Console URL: platform.claude.com/workspaces/<id>), or set the "
                "ANTHROPIC_WORKSPACE_ID environment variable."
            ) from exc
        raise AIUnavailable(f"Claude API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable("Could not reach the Claude API (network error).") from exc


def _call(settings: dict, tier: str, prompt: str, schema: dict,
          max_tokens: int = 4096, effort: str | None = None,
          thinking: bool = False) -> dict:
    """Make one call and log both sides of it at INFO.

    Once this is running in a container the log is the only window into what
    the model was asked and what it answered, so the prompt, any thinking it
    returns, and the raw JSON all go to stdout. `settings` never does: that
    dict carries the API key.
    """
    model = settings.get("models", {}).get(tier) or "claude-haiku-4-5"
    LOG.info(
        "AI call → tier=%s model=%s max_tokens=%s effort=%s thinking=%s",
        tier, model, max_tokens, effort or "default",
        "adaptive" if thinking else "off",
    )
    # The system prompt is the same on every call, so it is not worth a line
    # each time; ADDERALL_LOG_LEVEL=DEBUG brings it back when it matters.
    LOG.debug("AI system prompt:\n%s", SYSTEM)
    _log_text("AI input", prompt)
    try:
        response = _request(settings, model, prompt, schema, max_tokens,
                            effort, thinking)
    except AIUnavailable as exc:
        LOG.warning("AI call failed → tier=%s model=%s: %s", tier, model, exc)
        raise
    _log_response(response)
    if response.stop_reason == "refusal":
        raise AIUnavailable("The model declined this request.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise AIUnavailable("The model returned no usable output.")
    return json.loads(text)


def breakdown(settings: dict, title: str, description: str, granularity: int,
              parent_titles: list[str] | None = None) -> list[str]:
    """Magic ToDo: break one task into subtasks. Balanced tier, interactive."""
    granularity = min(5, max(1, int(granularity)))
    context = ""
    if parent_titles:
        context = f"This task is a subtask of: {' > '.join(parent_titles)}.\n"
    if description:
        context += f"Extra context from the user: {description}\n"
    prompt = (
        f"{context}Break this task into {GRANULARITY_HINTS[granularity]}, in the "
        f"order they should be done:\n\nTASK: {title}"
    )
    data = _call(settings, "balanced", prompt, BREAKDOWN_SCHEMA, effort="low")
    steps = [s.strip() for s in data.get("steps", []) if s.strip()]
    return steps[:MAX_STEPS]


def annotate(settings: dict, tasks: list[dict], want_scores: bool = True) -> dict[str, dict]:
    """Estimator + matrix seeding: one fast-tier call for a whole batch of
    tasks, returning {id: {minutes, impact?, effort?}}. Raw minutes only —
    the time-tax buffer is applied locally, never by the model."""
    if not tasks:
        return {}
    lines = []
    for t in tasks:
        desc = f" — {t['description']}" if t.get("description") else ""
        lines.append(f"- id={t['id']}: {t['title']}{desc}")
    fields = "estimated working minutes" + (
        ", impact 0-10, effort 0-10" if want_scores else ""
    )
    prompt = (
        f"For each task below, give {fields}. Estimate honestly; do not pad — "
        f"a buffer is added separately.\n\n" + "\n".join(lines)
    )
    data = _call(settings, "fast", prompt, annotate_schema(want_scores))
    valid_ids = {t["id"] for t in tasks}
    results: dict[str, dict] = {}
    for row in data.get("tasks", []):
        if row.get("id") not in valid_ids:
            continue
        # Bounds are enforced here rather than in the schema: structured
        # outputs reject numeric constraints, so the model can return
        # anything and the app must not store an out-of-range score.
        clean = {"id": row["id"], "minutes": _clamp(row.get("minutes"), 1, MAX_MINUTES)}
        if want_scores:
            clean["impact"] = _clamp(row.get("impact"), 0, 10)
            clean["effort"] = _clamp(row.get("effort"), 0, 10)
        results[row["id"]] = clean
    return results


def _clean_compiled(items, depth: int, budget: list[int]) -> list[dict]:
    """Normalise one level of the compiled tree, depth-first.

    `budget` is a single-element list shared across the whole walk so that
    MAX_COMPILED caps the tasks in the tree as a whole, not per level.
    Anything past the cap, past the depth, or missing a title is dropped.
    """
    out = []
    for item in items or []:
        if budget[0] <= 0:
            break
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        budget[0] -= 1
        node = {
            "title": title,
            "description": (item.get("description") or "").strip(),
            "subtasks": [],
        }
        if depth > 1:
            node["subtasks"] = _clean_compiled(item.get("subtasks"), depth - 1, budget)
        out.append(node)
    return out


def compile_braindump(settings: dict, text: str) -> list[dict]:
    """Compiler: one deep-tier call with adaptive thinking turns a messy
    braindump into a task tree [{title, description, subtasks}].

    Braindumps are rarely flat — "sort the car out" is really four steps —
    so the model is asked to group related items under the outcome they
    serve, and the app stores that shape as real parent/child tasks.
    """
    prompt = (
        "Turn this braindump into a list of discrete, actionable tasks. Merge "
        "duplicates, split compound items, drop non-task chatter. Keep titles "
        "short and imperative; put any useful detail from the braindump into "
        "the description.\n\n"
        "Nest the tasks. When several items are steps toward one outcome, or "
        "when one item is really a small project, make the outcome the parent "
        "task and put its steps in its `subtasks`. A parent's subtasks must be "
        f"the steps that finish it — nothing else. Nest at most {COMPILE_DEPTH} "
        "levels deep, and leave `subtasks` empty for anything that is already a "
        "single action. Keep unrelated one-off items at the top level: do not "
        "invent a parent to hold a single task, and do not group things "
        "together just because they were mentioned near each other.\n\n"
        f"Return at most {MAX_COMPILED} tasks in total, counting subtasks."
        "\n\nBRAINDUMP:\n" + text
    )
    data = _call(settings, "deep", prompt, COMPILE_SCHEMA, max_tokens=16000,
                 effort="high", thinking=True)
    return _clean_compiled(data.get("tasks"), COMPILE_DEPTH, [MAX_COMPILED])
