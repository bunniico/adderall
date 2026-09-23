"""Server-side transition alarms, webhooks, and the MCP server."""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
SETTINGS = {"alarms": {"enabled": True, "stop_lead": 30, "ready_lead": 10, "go_lead": 0}}


@pytest.fixture()
def events():
    from app import events
    events._fired.clear()
    events.recent.clear()
    events.subscribers.clear()
    return events


def task(deadline, id="t1", title="dentist"):
    return {"id": id, "title": title, "deadline": deadline.isoformat(),
            "project_id": "p1", "project_name": "Tasks"}


def test_each_stage_fires_once_inside_its_window(events):
    t = task(NOW + timedelta(minutes=10))
    fired = events.due_alarms([t], SETTINGS, NOW)
    assert [e["stage"] for e in fired] == ["ready"]
    assert fired[0]["text"] == "🧦 Get ready: “dentist”"
    assert fired[0]["task"]["project_name"] == "Tasks"
    # Checked again a tick later: already fired, so nothing.
    assert events.due_alarms([t], SETTINGS, NOW + timedelta(seconds=30)) == []
    # Ten minutes on, it is time to go.
    fired = events.due_alarms([t], SETTINGS, NOW + timedelta(minutes=10))
    assert [e["stage"] for e in fired] == ["go"]


def test_stale_cues_are_dropped_not_replayed(events):
    # The stop cue came due 20 minutes ago: far past the two-minute window.
    t = task(NOW + timedelta(minutes=10))
    assert "stop" not in [e["stage"] for e in events.due_alarms([t], SETTINGS, NOW)]


def test_disabled_alarms_fire_nothing(events):
    t = task(NOW)
    off = {"alarms": {**SETTINGS["alarms"], "enabled": False}}
    assert events.due_alarms([t], off, NOW) == []


def test_moving_a_deadline_arms_its_cues_again(events):
    assert events.due_alarms([task(NOW)], SETTINGS, NOW)
    moved = task(NOW + timedelta(seconds=30))
    assert [e["stage"] for e in events.due_alarms([moved], SETTINGS, NOW + timedelta(seconds=30))] == ["go"]


def test_publish_reaches_subscribers_history_and_webhooks(events, monkeypatch):
    sent = []

    async def fake_post(self, url, json):
        sent.append((url, json))
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    [event] = events.due_alarms([task(NOW)], SETTINGS, NOW)

    async def go():
        inbox = asyncio.Queue()
        events.subscribers.add(inbox)
        await events.publish(event, ["https://discord.com/api/webhooks/1/abc",
                                     "http://localhost:9000/hook", "  "])
        return inbox.get_nowait()

    assert asyncio.run(go()) == event
    assert list(events.recent) == [event]
    assert dict(sent) == {
        "https://discord.com/api/webhooks/1/abc": {"content": "🚀 Time for “dentist” — go now"},
        "http://localhost:9000/hook": event,
    }


def test_a_failing_webhook_does_not_stop_the_others(events, monkeypatch):
    sent = []

    async def fake_post(self, url, json):
        if "down" in url:
            raise httpx.ConnectError("refused")
        sent.append(url)
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    [event] = events.due_alarms([task(NOW)], SETTINGS, NOW)
    asyncio.run(events.publish(event, ["http://down/hook", "http://up/hook"]))
    assert sent == ["http://up/hook"]


# ---- through the app ----

@pytest.fixture()
def main(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    for name in ("ADDERALL_RECUR_INTERVAL", "ADDERALL_ALARM_INTERVAL",
                 "ADDERALL_CLICKUP_INTERVAL", "ADDERALL_QUEUE_INTERVAL"):
        monkeypatch.setenv(name, "0")
    from app import db
    importlib.reload(db)
    from app import main
    importlib.reload(main)
    # Hermetic: no AI behind a new task's estimate or its title's deadline.
    monkeypatch.setattr(main.ai, "annotate", lambda *a, **kw: {})
    monkeypatch.setattr(main.ai, "extract_schedule",
                        lambda settings, title, now_local: {"has_deadline": False,
                                                            "has_repeat": False})
    return main


def test_webhooks_setting_round_trips(main):
    client = TestClient(main.app)
    assert client.get("/api/settings").json()["webhooks"] == []
    urls = ["https://discord.com/api/webhooks/1/abc"]
    assert client.put("/api/settings", json={"webhooks": urls}).json()["webhooks"] == urls


def test_check_publishes_what_the_task_list_says_is_due(main, events):
    deadline = datetime.now(timezone.utc) - timedelta(seconds=30)
    alarm_tasks = [task(deadline)]
    fired = asyncio.run(events.check(lambda: alarm_tasks))
    assert [e["stage"] for e in fired] == ["go"]
    assert list(events.recent) == fired


def test_mcp_tools_work_the_task_list(main, events):
    headers = {"Accept": "application/json, text/event-stream"}

    def rpc(client, method, params=None, id=1):
        res = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})
        assert res.status_code == 200, res.text
        return res.json()

    def call(client, name, **arguments):
        result = rpc(client, "tools/call", {"name": name, "arguments": arguments})["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]["result"]

    with TestClient(main.app, base_url="http://localhost:8000") as client:
        rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}})
        names = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
        assert {"list_tasks", "add_task", "complete_task", "recent_alarms"} <= names

        tree = call(client, "add_task", title="water the plants", estimated_time=5)
        [plants] = [t for t in tree if t["title"] == "water the plants"]
        tree = call(client, "complete_task", task_id=plants["id"])
        assert next(t for t in tree if t["id"] == plants["id"])["status"] == "done"
        assert call(client, "recent_alarms") == []

        missing = rpc(client, "tools/call", {"name": "start_task",
                                             "arguments": {"task_id": "nope"}})
        assert missing["result"]["isError"]
        assert "not found" in missing["result"]["content"][0]["text"].lower()

        bad = rpc(client, "tools/call", {"name": "update_task", "arguments": {
            "task_id": plants["id"], "changes": {"impact": 99}}})
        assert bad["result"]["isError"]
        assert "impact" in bad["result"]["content"][0]["text"]
