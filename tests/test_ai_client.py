"""Client construction: identity-linked keys need an anthropic-workspace-id."""

import importlib

import anthropic
import pytest

from app import ai


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Every call books what it cost, so these tests need somewhere to book it."""
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    from app import db
    importlib.reload(db)
    db.init()


def test_workspace_header_from_settings(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    client = ai._client({"api_key": "sk-ant-test", "workspace_id": "wrkspc_123"})
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_123"


def test_workspace_header_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_env")
    client = ai._client({"api_key": "sk-ant-test", "workspace_id": ""})
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_env"


def test_settings_workspace_wins_over_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_env")
    client = ai._client({"api_key": "sk-ant-test", "workspace_id": "wrkspc_settings"})
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_settings"


def test_no_workspace_header_when_unset(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    client = ai._client({"api_key": "sk-ant-test"})
    assert "anthropic-workspace-id" not in client.default_headers


def test_workspace_error_gets_actionable_message(monkeypatch):
    """A 400 naming the header explains the fix instead of echoing raw JSON."""
    class FakeResponse:
        status_code = 400
        headers = {}
        request = None

    def boom(**kwargs):
        raise anthropic.BadRequestError(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'anthropic-workspace-id is "
            "required when authenticating with an identity-linked API key; "
            "send the id of the workspace this request acts in.'}}",
            response=FakeResponse(),
            body=None,
        )

    class FakeClient:
        class messages:
            create = staticmethod(boom)

    monkeypatch.setattr(ai, "_client", lambda settings: FakeClient())
    with pytest.raises(ai.AIUnavailable) as exc:
        ai._call({}, "fast", "hi", {"type": "object"})
    assert "Settings → Workspace ID" in str(exc.value)
    assert "ANTHROPIC_WORKSPACE_ID" in str(exc.value)


# ---------- logging ----------

class FakeBlock:
    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class FakeUsage:
    input_tokens = 12
    output_tokens = 34


class FakeResponse:
    model = "claude-opus-5"
    stop_reason = "end_turn"
    usage = FakeUsage()

    def __init__(self, content):
        self.content = content


def _fake_client(monkeypatch, response, seen=None):
    """Point ai._client at a client that returns `response`, recording kwargs."""
    class Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                if seen is not None:
                    seen.update(kwargs)
                return response

    monkeypatch.setattr(ai, "_client", lambda settings: Client())


def test_logs_prompt_thinking_and_output(monkeypatch, caplog):
    response = FakeResponse([
        FakeBlock("thinking", thinking="Three of these are one outcome."),
        FakeBlock("text", text='{"steps": ["Do the thing"]}'),
    ])
    _fake_client(monkeypatch, response)
    with caplog.at_level("INFO", logger="app.ai"):
        assert ai._call({"api_key": "sk-ant-secret"}, "deep", "BRAINDUMP: laundry",
                        {"type": "object"}, thinking=True) == {"steps": ["Do the thing"]}
    log = caplog.text
    assert "AI input:\nBRAINDUMP: laundry" in log
    assert "AI thinking:\nThree of these are one outcome." in log
    assert 'AI output:\n{"steps": ["Do the thing"]}' in log
    assert "stop_reason=end_turn" in log
    # The prompt and the completion are informational; the key never is.
    assert "sk-ant-secret" not in log


def test_thinking_asks_for_a_summary(monkeypatch):
    """Adaptive thinking left on its default display logs nothing useful."""
    seen: dict = {}
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]), seen)
    ai._call({}, "deep", "hi", {"type": "object"}, thinking=True)
    assert seen["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_long_text_is_truncated(monkeypatch, caplog):
    monkeypatch.setenv("ADDERALL_AI_LOG_CHARS", "20")
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]))
    with caplog.at_level("INFO", logger="app.ai"):
        ai._call({}, "fast", "x" * 60, {"type": "object"})
    assert f"{'x' * 20}… [40 more characters]" in caplog.text


def test_failure_is_logged(monkeypatch, caplog):
    def boom(**kwargs):
        raise anthropic.APIConnectionError(request=None)

    class Client:
        class messages:
            create = staticmethod(boom)

    monkeypatch.setattr(ai, "_client", lambda settings: Client())
    with caplog.at_level("WARNING", logger="app.ai"), pytest.raises(ai.AIUnavailable):
        ai._call({}, "fast", "hi", {"type": "object"})
    assert "AI call failed" in caplog.text


# ---------- what a call costs, and what that costs it ----------

MODELS = {"fast": "claude-haiku-4-5", "balanced": "claude-sonnet-5",
          "deep": "claude-opus-5"}


class CachedUsage:
    input_tokens = 1000
    output_tokens = 500
    cache_creation_input_tokens = 400
    cache_read_input_tokens = 2000


def test_cost_is_tokens_times_the_list_price():
    """1000 input and 500 output on Opus 5: $5 and $25 per million."""
    class Usage:
        input_tokens = 1000
        output_tokens = 500
    assert ai.usage_cost("claude-opus-5", Usage()) == pytest.approx(0.0175)
    assert ai.usage_cost("claude-haiku-4-5", Usage()) == pytest.approx(0.0035)


def test_cached_input_is_billed_off_the_input_rate():
    """A write costs a quarter more than fresh input, a hit a tenth as much —
    which is the only reason the cache-marked system prompt is worth having."""
    # Sonnet 5 at $2/$10: (1000 + 400*1.25 + 2000*0.1) * 2 + 500 * 10, per 1M.
    assert ai.usage_cost("claude-sonnet-5", CachedUsage()) == pytest.approx(0.00840)


def test_an_unknown_model_is_costed_at_the_dearest_rate_known():
    """A budget that guesses low is a budget that does not hold, so a name the
    table has never seen is never cheaper than one it has."""
    guess = ai.model_price("claude-something-new")
    assert all(guess[0] >= known[0] and guess[1] >= known[1]
               for known in ai.PRICES.values())


def test_a_response_with_no_usage_costs_nothing():
    assert ai.usage_cost("claude-opus-5", None) == 0.0


def test_every_call_books_what_it_cost(monkeypatch):
    from app import db
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]))
    ai._call({"models": MODELS}, "fast", "hi", {"type": "object"})
    # FakeUsage is 12 in / 34 out on Haiku 4.5 — small, but not nothing.
    assert db.spend_since("2000-01-01T00:00:00+00:00") == pytest.approx(0.000182)


def test_without_a_budget_nothing_is_throttled(monkeypatch):
    seen: dict = {}
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]), seen)
    ai._call({"models": MODELS}, "deep", "hi", {"type": "object"},
             effort="high", thinking=True)
    assert seen["model"] == "claude-opus-5"
    assert seen["output_config"]["effort"] == "high"


def test_spending_half_the_budget_moves_braindumps_off_the_deep_model(monkeypatch):
    from app import db
    db.update_settings({"daily_budget_usd": 1.0})
    db.record_spend("deep", "claude-opus-5", 0.60)
    seen: dict = {}
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]), seen)
    ai._call(db.get_settings() | {"models": MODELS}, "deep", "hi",
             {"type": "object"}, effort="high", thinking=True)
    assert seen["model"] == "claude-sonnet-5"


def test_a_throttled_call_drops_thinking_and_effort_too(monkeypatch):
    """The cheap tier's treatment as well as its model: those knobs are what
    the dear tiers are for, and the fast tier's model rejects them outright."""
    from app import db
    db.update_settings({"daily_budget_usd": 1.0})
    db.record_spend("deep", "claude-opus-5", 1.20)
    seen: dict = {}
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]), seen)
    ai._call(db.get_settings() | {"models": MODELS}, "deep", "hi",
             {"type": "object"}, effort="high", thinking=True)
    assert seen["model"] == "claude-haiku-4-5"
    assert "thinking" not in seen
    assert "effort" not in seen["output_config"]


def test_the_throttle_says_so_in_the_log(monkeypatch, caplog):
    from app import db
    db.update_settings({"daily_budget_usd": 1.0})
    db.record_spend("deep", "claude-opus-5", 1.20)
    _fake_client(monkeypatch, FakeResponse([FakeBlock("text", text="{}")]))
    with caplog.at_level("INFO", logger="app.ai"):
        ai._call(db.get_settings() | {"models": MODELS}, "balanced", "hi",
                 {"type": "object"})
    assert "throttled to fast" in caplog.text
    assert "the daily budget is spent" in caplog.text
