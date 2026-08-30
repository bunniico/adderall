"""Client construction: identity-linked keys need an anthropic-workspace-id."""

import anthropic
import pytest

from app import ai


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
