"""💬 Ask: the chat loop, with the Anthropic client faked (no network)."""

import importlib
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app import chat as chatmod


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    from app import db
    importlib.reload(db)
    from app import main
    importlib.reload(main)
    db.update_settings({"title_parsing_ai": False, "ai_scoring": False,
                        "modules": {"ai_estimates": False}})
    return main


def text(t):
    return NS(type="text", text=t)


def tool(name, args, id="tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id)


def reply(*content, stop="end_turn"):
    usage = NS(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
               cache_read_input_tokens=0)
    return NS(content=list(content), stop_reason=stop, usage=usage, model="m")


class FakeClient:
    """Hands back scripted responses and remembers what it was asked."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def fake(monkeypatch, main, script):
    client = FakeClient(script)
    monkeypatch.setattr(chatmod.ai, "_client", lambda settings: client)
    return client


def ask(app, text_, history=()):
    client = TestClient(app.app)
    return client.post("/api/chat", json={"messages": [
        *history, {"role": "user", "content": text_}]})


def test_a_question_is_answered_from_the_help_without_acting(app, monkeypatch):
    client = fake(monkeypatch, app, [reply(text("The time tax pads every estimate."))])
    res = ask(app, "what is the time tax?")
    assert res.status_code == 200
    body = res.json()
    assert body["reply"] == "The time tax pads every estimate."
    assert body["actions"] == [] and body["changed"] is False and "state" not in body
    call = client.calls[0]
    system = call["system"][0]
    assert "# Estimates and the time tax" in system["text"]   # the help is in it
    assert system["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in call["tools"]} >= {"add_task", "list_tasks", "complete_task"}


def test_an_action_runs_the_mcp_tool_and_returns_the_new_state(app, monkeypatch):
    client = fake(monkeypatch, app, [
        reply(text("Adding it."), tool("add_task", {"title": "buy milk"}), stop="tool_use"),
        reply(text("Added “buy milk”.")),
    ])
    body = ask(app, "add buy milk to my list").json()
    assert body["reply"] == "Added “buy milk”."
    assert body["actions"] == [{"tool": "add_task", "ok": True}]
    assert body["changed"] is True
    assert any(t["title"] == "buy milk" for t in body["state"]["tasks"])
    # The whole assistant turn went back, then the result, in one message.
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
    assert "buy milk" in result["content"]


def test_a_failing_tool_is_reported_to_the_model_not_raised(app, monkeypatch):
    client = fake(monkeypatch, app, [
        reply(tool("delete_task", {"task_id": "nope"}), stop="tool_use"),
        reply(text("I couldn't find that task.")),
    ])
    body = ask(app, "delete task nope").json()
    assert body["actions"] == [{"tool": "delete_task", "ok": False}]
    assert body["changed"] is False
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "not found" in result["content"].lower()


def test_reading_tools_do_not_count_as_a_change(app, monkeypatch):
    fake(monkeypatch, app, [
        reply(tool("list_tasks", {}), stop="tool_use"),
        reply(text("Your list is empty.")),
    ])
    body = ask(app, "what's on my list?").json()
    assert body["actions"] == [{"tool": "list_tasks", "ok": True}]
    assert body["changed"] is False


def test_a_conversation_that_never_settles_stops(app, monkeypatch):
    loop = [reply(tool("next_task", {}, id=f"t{i}"), stop="tool_use")
            for i in range(chatmod.MAX_STEPS)]
    client = fake(monkeypatch, app, loop)
    body = ask(app, "keep going forever").json()
    assert len(client.calls) == chatmod.MAX_STEPS
    assert "more steps" in body["reply"]


def test_history_is_sent_and_must_end_with_the_user(app, monkeypatch):
    client = fake(monkeypatch, app, [reply(text("Sure."))])
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "Hello!"}]
    assert ask(app, "and again", history).status_code == 200
    assert [m["role"] for m in client.calls[0]["messages"]] == ["user", "assistant", "user"]
    bad = TestClient(app.app).post("/api/chat", json={"messages": history})
    assert bad.status_code == 400


def test_api_errors_become_a_502_with_a_message(app, monkeypatch):
    class NoKey:
        messages = NS(create=lambda **kw: (_ for _ in ()).throw(TypeError("no creds")))

    monkeypatch.setattr(chatmod.ai, "_client", lambda settings: NoKey())
    res = ask(app, "hello?")
    assert res.status_code == 502
    assert "API key" in res.json()["detail"]


def test_the_chat_is_a_module(app, monkeypatch):
    fake(monkeypatch, app, [reply(text("hi"))])
    from app import db
    db.update_settings({"modules": {"ai_chat": False}})
    assert ask(app, "hello").status_code == 404


def test_spend_is_booked_for_every_call(app, monkeypatch):
    fake(monkeypatch, app, [
        reply(tool("next_task", {}), stop="tool_use"),
        reply(text("Nothing to do.")),
    ])
    from app import db
    before = db.spend_since("1970-01-01T00:00:00+00:00")
    ask(app, "what next?")
    assert db.spend_since("1970-01-01T00:00:00+00:00") > before
