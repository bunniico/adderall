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

Every call is costed against the price table below and booked, and a daily
budget can pull the routing down a tier at a time as the day's spend climbs —
see `_call`.
"""

from __future__ import annotations

import json
import logging
import os

import anthropic

from . import db, logic

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
    "executive load, not just duration. A start time says when the person "
    "would sensibly begin a task, which is a judgment about the task itself: "
    "'eat dinner' belongs to this evening, 'renew the passport' to some "
    "weekday soon, 'finish that game' to whenever there is nothing better on. "
    "Return only the requested JSON."
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
# How far ahead a suggested start time may sit: a year. "Some day" has to have
# a day on it for the scheduler to place it at all, and a year out is already
# far past the point where the answer is "not now" and nothing more.
MAX_START_MINUTES = 60 * 24 * 365

BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["steps"],
    "additionalProperties": False,
}

# No nullable fields — structured outputs carry types only (see the note on
# BREAKDOWN_SCHEMA's neighbours below) — so "not found" is its own boolean
# rather than a null, and the value fields are simply ignored when it is set.
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "has_deadline": {"type": "boolean"},
        "deadline_in_minutes": {"type": "integer"},
        "has_repeat": {"type": "boolean"},
        "repeat_freq": {"type": "string"},
        "repeat_interval": {"type": "integer"},
        "repeat_weekdays": {"type": "array", "items": {"type": "integer"}},
        "clean_title": {"type": "string"},
    },
    "required": ["has_deadline", "deadline_in_minutes", "has_repeat", "repeat_freq",
                "repeat_interval", "repeat_weekdays", "clean_title"],
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


def annotate_schema(want_scores: bool = True, want_start: bool = False) -> dict:
    props: dict = {"id": {"type": "string"}, "minutes": {"type": "integer"}}
    required = ["id", "minutes"]
    if want_scores:
        props["impact"] = {"type": "integer"}
        props["effort"] = {"type": "integer"}
        required += ["impact", "effort"]
    if want_start:
        # Minutes from now, not a date. The model is told what the local time
        # is and answers with an offset, which spares it — and this schema —
        # every timezone, format and end-of-month trap there is; the app turns
        # it back into an instant, which is the only thing it stores.
        props["start_in_minutes"] = {"type": "integer"}
        required.append("start_in_minutes")
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


# ---------- what a call cost ----------
# List prices in dollars per million tokens, (input, output). Approximate on
# purpose: this is here to keep a daily budget honest, not to reconcile an
# invoice, and it will drift as Anthropic's price list changes. Cached input
# is billed off the input rate — a write costs a quarter more than reading the
# tokens fresh, a hit a tenth as much — which is the whole reason the system
# prompt above is cache-marked.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_WRITE_RATE = 1.25   # writing the cache, as a multiple of the input rate
CACHE_READ_RATE = 0.10    # reading it back
PER_MILLION = 1_000_000

# What a model the table has never heard of costs: the dearest rate known, on
# each axis separately. A budget that overestimates makes the app cautious;
# one that underestimates makes the budget a lie, and the point of the number
# is that it holds.
UNKNOWN_PRICE = (max(price[0] for price in PRICES.values()),
                 max(price[1] for price in PRICES.values()))


def model_price(model: str) -> tuple[float, float]:
    """(input, output) dollars per million tokens for `model`.

    Anything off the table — a new release, or a name typed into the settings
    — is costed at `UNKNOWN_PRICE`.
    """
    return PRICES.get(model, UNKNOWN_PRICE)


def usage_cost(model: str, usage) -> float:
    """What one response cost, in dollars, from the token counts it reports."""
    if usage is None:
        return 0.0
    def tokens(field: str) -> int:
        try:
            return max(0, int(getattr(usage, field, 0) or 0))
        except (TypeError, ValueError):
            return 0
    price_in, price_out = model_price(model)
    billed_in = (tokens("input_tokens")
                 + tokens("cache_creation_input_tokens") * CACHE_WRITE_RATE
                 + tokens("cache_read_input_tokens") * CACHE_READ_RATE)
    return (billed_in * price_in + tokens("output_tokens") * price_out) / PER_MILLION


def throttle_stage(settings: dict) -> int:
    """How far down the cheap end of the ladder today's spend has pushed us.

    0 whenever no budget is set, which is the default and skips the query
    entirely: an app nobody has given a number to does not go looking for one.
    """
    budget = logic.daily_budget(settings)
    if budget <= 0:
        return 0
    spent = db.spend_since(logic.spend_window_start(settings))
    return logic.throttle_stage(spent, budget)


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

    This is also the one place a call is routed, so it is where a daily budget
    gets its way: `tier` is what the caller wanted and `served` is what today's
    spend can afford. A throttled call is given the cheaper tier's treatment
    entire — no thinking, no effort — because those are what the expensive
    tiers are for, and because the cheap tier's model may reject them outright.
    """
    stage = throttle_stage(settings)
    served = logic.throttled_tier(tier, stage)
    if served != tier:
        effort, thinking = None, False
    model = settings.get("models", {}).get(served) or "claude-haiku-4-5"
    LOG.info(
        "AI call → tier=%s%s model=%s max_tokens=%s effort=%s thinking=%s",
        tier, f" (throttled to {served}: {logic.THROTTLE_NOTES[stage]})"
        if served != tier else "",
        model, max_tokens, effort or "default",
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
    # Booked before the response is unpacked: a reply the app cannot parse was
    # still generated, and still billed.
    cost = usage_cost(model, getattr(response, "usage", None))
    LOG.info("AI cost ≈ $%.4f (model=%s)", cost, model)
    db.record_spend(served, model, cost)
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


EXTRACT_GUIDANCE = (
    "The title of a new task may say, in plain English, when it is due or how "
    "often it repeats — 'renew the passport by mid-October', 'water the "
    "plants every other day', 'call the dentist sometime next week'. Obvious "
    "keyword phrasing ('tomorrow', 'every day', 'next monday') is already "
    "handled elsewhere and will not reach you here — only loosely-worded "
    "phrasing does, so read generously.\n"
    "If you find a deadline, set has_deadline=true and deadline_in_minutes to "
    "how many minutes from right now it falls (use the local time above). If "
    "you find a repeat phrase, set has_repeat=true, repeat_freq to one of "
    "daily/weekly/monthly/yearly, repeat_interval to 1 for a plain 'every X' "
    "or N for 'every N Xs', and repeat_weekdays (0=Sunday..6=Saturday) only "
    "when specific named weekdays were given, otherwise leave it empty. If "
    "nothing schedule-related is in the title, set both has_deadline and "
    "has_repeat to false. clean_title is always required: the title with "
    "whatever schedule phrase you found removed and any leftover punctuation "
    "tidied up, or the title completely unchanged if nothing was found."
)


def extract_schedule(settings: dict, title: str, now_local: str) -> dict:
    """Fallback for `title_parse.parse_title`: phrasing too loose for the
    local regex parser, read by the fast tier instead.

    Only called when the regex pass already came up empty, so this is a rare
    call rather than one on every task — the annotate call already happening
    on task creation is where estimates and scores come from; this is purely
    for schedule words the fixed vocabulary missed.
    """
    prompt = (f"The local date and time right now is {now_local}.\n\n"
             + EXTRACT_GUIDANCE + f"\n\nTITLE: {title}")
    data = _call(settings, "fast", prompt, EXTRACT_SCHEMA)
    result = {"has_deadline": bool(data.get("has_deadline")),
             "deadline_in_minutes": _clamp(data.get("deadline_in_minutes"),
                                           0, MAX_START_MINUTES),
             "has_repeat": bool(data.get("has_repeat")) and
                          data.get("repeat_freq") in logic.RECUR_FREQS,
             "repeat_freq": data.get("repeat_freq"),
             "repeat_interval": _clamp(data.get("repeat_interval") or 1,
                                       1, logic.RECUR_MAX_INTERVAL),
             "repeat_weekdays": sorted({int(d) for d in data.get("repeat_weekdays", [])
                                        if isinstance(d, int) and 0 <= d <= 6}),
             "clean_title": (data.get("clean_title") or title).strip() or title}
    return result


# What the model is told a start time is for. Spelled out at length because
# it is the one field here that is a scheduling decision rather than a
# measurement, and a vague prompt gets "tomorrow" for everything.
START_GUIDANCE = (
    "Also give start_in_minutes: how many minutes from right now the person "
    "should ideally BEGIN the task. This is not a deadline and not how long it "
    "takes — it is which slot in the coming days or weeks the task belongs in, "
    "and the app schedules the task from it.\n"
    "- Anything that plainly belongs to today or this evening — meals, meds, "
    "feeding the cat, an errand before the shops shut — goes within the next "
    "few hours, at the hour it actually happens. Use the local time above to "
    "work that offset out.\n"
    "- Time-of-day-bound work with no urgency (a call that needs office hours) "
    "goes to the next sensible slot in working hours.\n"
    "- Ordinary tasks with no natural hour go somewhere in the next few days.\n"
    "- Things that genuinely do not matter when they happen — a long game, a "
    "hobby project, 'read that book' — go weeks or months out. Say so with a "
    "large number rather than a small one: putting them near the front takes "
    "the good hours away from work that needed them.\n"
    "- 0 means start now, and should be rare."
)


def annotate(settings: dict, tasks: list[dict], want_scores: bool = True,
             want_start: bool = False, now_local: str = "") -> dict[str, dict]:
    """Estimator + matrix seeding: one fast-tier call for a whole batch of
    tasks, returning {id: {minutes, impact?, effort?, start_in_minutes?}}.

    Raw minutes only — the time-tax buffer is applied locally, never by the
    model — and start times come back as an offset from `now_local`, which is
    the caller's own clock rendered for the prompt.
    """
    if not tasks:
        return {}
    want_start = want_start and bool(now_local)
    lines = []
    for t in tasks:
        desc = f" — {t['description']}" if t.get("description") else ""
        lines.append(f"- id={t['id']}: {t['title']}{desc}")
    fields = "estimated working minutes" + (
        ", impact 0-10, effort 0-10" if want_scores else ""
    )
    prompt = ""
    if want_start:
        prompt += f"The local date and time right now is {now_local}.\n\n"
    prompt += (
        f"For each task below, give {fields}. Estimate honestly; do not pad — "
        f"a buffer is added separately.\n"
    )
    if want_start:
        prompt += START_GUIDANCE + "\n"
    prompt += "\n" + "\n".join(lines)
    data = _call(settings, "fast", prompt, annotate_schema(want_scores, want_start))
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
        if want_start and row.get("start_in_minutes") is not None:
            clean["start_in_minutes"] = _clamp(
                row.get("start_in_minutes"), 0, MAX_START_MINUTES)
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
