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
