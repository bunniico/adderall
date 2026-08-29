"""API tests against a temp database, with the AI layer stubbed out."""

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    from app import db
    importlib.reload(db)
    from app import ai, logic, main
    importlib.reload(main)

    # Stub the AI so tests are hermetic.
    monkeypatch.setattr(main.ai, "annotate",
                        lambda settings, tasks, want_scores=True: {
                            t["id"]: {"id": t["id"], "minutes": 30, "impact": 7, "effort": 3}
                            for t in tasks
                        })
    monkeypatch.setattr(main.ai, "breakdown",
                        lambda settings, title, desc, granularity, parents=None:
                        [f"step {i}" for i in range(1, 4)])
    monkeypatch.setattr(main.ai, "compile_braindump",
                        lambda settings, text: [
                            {"title": "call dentist", "description": ""},
                            {"title": "buy gift", "description": "for mom"},
                        ])
    return TestClient(main.app)


def create(client, **kw):
    body = {"title": "test task", **kw}
    res = client.post("/api/tasks", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def find(state, title):
    def walk(tasks):
        for t in tasks:
            if t["title"] == title:
                return t
            got = walk(t["subtasks"])
            if got:
                return got
    return walk(state["tasks"])


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_create_task_persists_and_annotates(client):
    state = create(client, title="wash dishes")
    task = find(state, "wash dishes")
    assert task["estimated_time"] == 30
    assert task["impact"] == 7 and task["effort"] == 3
    assert task["quadrant"] == "quick_win"
    assert task["buffered_estimate"] == 39  # 30 * 1.3
    # survives a "restart" (new request, same db)
    again = client.get("/api/state").json()
    assert find(again, "wash dishes")


def test_manual_values_not_overwritten_by_annotation(client):
    state = create(client, title="manual", estimated_time=90, impact=2, effort=9)
    task = find(state, "manual")
    assert task["estimated_time"] == 90
    assert task["impact"] == 2 and task["effort"] == 9
    assert task["quadrant"] == "thankless"


def test_breakdown_creates_subtasks(client):
    state = create(client, title="clean kitchen")
    tid = find(state, "clean kitchen")["id"]
    res = client.post(f"/api/tasks/{tid}/breakdown", json={"granularity": 3})
    assert res.status_code == 200
    parent = find(res.json(), "clean kitchen")
    assert [s["title"] for s in parent["subtasks"]] == ["step 1", "step 2", "step 3"]
    # subtasks got annotated too
    assert all(s["estimated_time"] == 30 for s in parent["subtasks"])


def test_complete_parent_completes_children(client):
    state = create(client, title="parent")
    tid = find(state, "parent")["id"]
    client.post(f"/api/tasks/{tid}/breakdown", json={})
    res = client.post(f"/api/tasks/{tid}/complete", json={})
    parent = find(res.json(), "parent")
    assert parent["status"] == "done"
    assert all(s["status"] == "done" for s in parent["subtasks"])


def test_complete_records_actual_time(client):
    state = create(client, title="timed")
    tid = find(state, "timed")["id"]
    res = client.post(f"/api/tasks/{tid}/complete", json={"actual_time": 42})
    assert find(res.json(), "timed")["actual_time"] == 42


def test_update_and_delete(client):
    state = create(client, title="old name")
    tid = find(state, "old name")["id"]
    res = client.patch(f"/api/tasks/{tid}", json={"title": "new name", "status": "discarded"})
    assert find(res.json(), "new name")["status"] == "discarded"
    res = client.delete(f"/api/tasks/{tid}")
    assert find(res.json(), "new name") is None
    assert client.delete(f"/api/tasks/{tid}").status_code == 404


def test_compile_braindump(client):
    res = client.post("/api/compile", json={"text": "dentist... mom bday..."})
    assert res.status_code == 200
    state = res.json()
    assert find(state, "call dentist")
    assert find(state, "buy gift")["description"] == "for mom"


def test_next_endpoint(client):
    create(client, title="only task")
    res = client.get("/api/next").json()
    assert res["task"]["title"] == "only task"


def test_settings_roundtrip_and_key_privacy(client):
    res = client.get("/api/settings").json()
    assert "api_key" not in res
    assert res["buffer"] == 0.30
    res = client.put("/api/settings", json={
        "buffer": 0.5, "auto_deadlines": False,
        "alarms": {"stop_lead": 45}, "api_key": "sk-ant-secret",
    }).json()
    assert res["buffer"] == 0.5
    assert res["auto_deadlines"] is False
    assert res["alarms"]["stop_lead"] == 45
    assert res["alarms"]["ready_lead"] == 10  # untouched nested default
    assert res["has_api_key"] is True
    assert "api_key" not in res  # never echoed back


def test_workspace_id_roundtrips_and_clears(client):
    res = client.put("/api/settings", json={"workspace_id": "wrkspc_abc"}).json()
    assert res["workspace_id"] == "wrkspc_abc"  # not a secret, safe to echo
    assert client.get("/api/settings").json()["workspace_id"] == "wrkspc_abc"
    # blank clears it (unlike the API key, where blank means "keep current")
    assert client.put("/api/settings", json={"workspace_id": ""}).json()["workspace_id"] == ""


def test_auto_deadline_respects_toggle(client):
    client.put("/api/settings", json={"auto_deadlines": False})
    state = create(client, title="floaty")
    task = find(state, "floaty")
    assert task["deadline"] is None
    assert task["deadline_source"] == "none"


def test_ai_failure_does_not_block_creation(client, monkeypatch):
    from app import main
    def boom(*a, **kw):
        raise main.ai.AIUnavailable("no key")
    monkeypatch.setattr(main.ai, "annotate", boom)
    state = create(client, title="offline task")
    task = find(state, "offline task")
    assert task is not None
    assert task["estimated_time"] is None


def test_breakdown_ai_failure_returns_502(client, monkeypatch):
    from app import main
    def boom(*a, **kw):
        raise main.ai.AIUnavailable("no key")
    monkeypatch.setattr(main.ai, "breakdown", boom)
    state = create(client, title="x")
    tid = find(state, "x")["id"]
    res = client.post(f"/api/tasks/{tid}/breakdown", json={})
    assert res.status_code == 502
    assert "no key" in res.json()["detail"]


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "adderall" in res.text
