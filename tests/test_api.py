"""API tests against a temp database, with the AI layer stubbed out."""

import importlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

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


def test_manual_subtask_under_any_task(client):
    state = create(client, title="parent")
    parent = find(state, "parent")
    state = create(client, title="by hand", parent_id=parent["id"])
    parent = find(state, "parent")
    assert [s["title"] for s in parent["subtasks"]] == ["by hand"]
    assert parent["has_subtasks"] is True
    # and a subtask of a subtask, to any depth
    sub = find(state, "by hand")
    state = create(client, title="deeper", parent_id=sub["id"])
    assert [s["title"] for s in find(state, "by hand")["subtasks"]] == ["deeper"]


def test_manual_subtask_appends_after_existing_ones(client):
    state = create(client, title="parent")
    tid = find(state, "parent")["id"]
    client.post(f"/api/tasks/{tid}/breakdown", json={})
    state = create(client, title="one more", parent_id=tid)
    assert [s["title"] for s in find(state, "parent")["subtasks"]] == [
        "step 1", "step 2", "step 3", "one more"]


def test_subtask_under_unknown_parent_is_404(client):
    assert client.post("/api/tasks", json={"title": "x", "parent_id": "nope"}
                       ).status_code == 404


def roots(state):
    """Top-level titles in the order the page shows them (see sortActive)."""
    return [t["title"] for t in sorted(state["tasks"], key=lambda t: t["sort_key"])]


def test_move_reorders_top_level_tasks(client):
    for title in ("a", "b", "c"):
        state = create(client, title=title)
    ids = {t["title"]: t["id"] for t in state["tasks"]}
    state = client.post(f"/api/tasks/{ids['c']}/move",
                        json={"target_id": ids["a"], "mode": "before"}).json()
    assert roots(state) == ["c", "a", "b"]
    state = client.post(f"/api/tasks/{ids['c']}/move",
                        json={"target_id": ids["b"], "mode": "after"}).json()
    assert roots(state) == ["a", "b", "c"]


def test_move_nests_and_unnests(client):
    state = create(client, title="a")
    state = create(client, title="b")
    ids = {t["title"]: t["id"] for t in state["tasks"]}
    state = client.post(f"/api/tasks/{ids['b']}/move",
                        json={"target_id": ids["a"], "mode": "into"}).json()
    assert roots(state) == ["a"]
    assert [s["title"] for s in find(state, "a")["subtasks"]] == ["b"]
    # dropping on empty list space pulls it back out to the top level
    state = client.post(f"/api/tasks/{ids['b']}/move",
                        json={"parent_id": None, "position": None}).json()
    assert roots(state) == ["a", "b"]
    assert find(state, "a")["subtasks"] == []


def test_move_out_places_task_after_its_old_parent(client):
    state = create(client, title="project")
    pid = find(state, "project")["id"]
    state = client.post(f"/api/tasks/{pid}/breakdown", json={}).json()
    step1 = find(state, "step 1")["id"]
    state = client.post(f"/api/tasks/{step1}/move",
                        json={"target_id": pid, "mode": "after"}).json()
    assert roots(state) == ["project", "step 1"]
    assert [s["title"] for s in find(state, "project")["subtasks"]] == ["step 2", "step 3"]


def test_move_position_is_relative_to_the_list_without_the_moved_task(client):
    state = create(client, title="parent")
    pid = find(state, "parent")["id"]
    state = client.post(f"/api/tasks/{pid}/breakdown", json={}).json()
    subs = {s["title"]: s["id"] for s in find(state, "parent")["subtasks"]}
    # move the first step down past the second — a naive index would no-op
    state = client.post(f"/api/tasks/{subs['step 1']}/move",
                        json={"target_id": subs["step 2"], "mode": "after"}).json()
    assert [s["title"] for s in find(state, "parent")["subtasks"]] == [
        "step 2", "step 1", "step 3"]


def test_move_rejects_cycles_and_unknown_tasks(client):
    state = create(client, title="parent")
    pid = find(state, "parent")["id"]
    state = client.post(f"/api/tasks/{pid}/breakdown", json={}).json()
    step1 = find(state, "step 1")["id"]

    res = client.post(f"/api/tasks/{pid}/move", json={"target_id": step1, "mode": "into"})
    assert res.status_code == 400 and "own subtasks" in res.json()["detail"]
    res = client.post(f"/api/tasks/{pid}/move", json={"target_id": pid, "mode": "into"})
    assert res.status_code == 400
    assert client.post(f"/api/tasks/{pid}/move",
                       json={"parent_id": "nope"}).status_code == 404
    assert client.post("/api/tasks/nope/move", json={}).status_code == 404
    assert client.post(f"/api/tasks/{pid}/move",
                       json={"target_id": step1}).status_code == 400  # mode missing
    # nothing was moved by any of the rejections
    assert [s["title"] for s in find(client.get("/api/state").json(), "parent")[
        "subtasks"]] == ["step 1", "step 2", "step 3"]


def test_first_move_freezes_the_order_you_were_looking_at(client):
    # urgent sorts to the top under auto-sort, even though it was added last
    create(client, title="calm one", impact=8, effort=2, estimated_time=10)
    create(client, title="middling", impact=5, effort=5, estimated_time=10)
    state = create(client, title="urgent", estimated_time=60,
                   deadline=(datetime.now(timezone.utc) + timedelta(minutes=70)).isoformat())
    assert roots(state) == ["urgent", "calm one", "middling"]
    assert client.get("/api/settings").json()["manual_order"] is False

    ids = {t["title"]: t["id"] for t in state["tasks"]}
    state = client.post(f"/api/tasks/{ids['middling']}/move",
                        json={"target_id": ids["urgent"], "mode": "before"}).json()
    # manual order took over, and only the dragged task moved
    assert client.get("/api/settings").json()["manual_order"] is True
    assert roots(state) == ["middling", "urgent", "calm one"]
    assert state["next_task_id"] == ids["middling"]


def test_manual_order_can_be_switched_back_off(client):
    create(client, title="calm one", impact=8, effort=2, estimated_time=10)
    state = create(client, title="urgent", estimated_time=60,
                   deadline=(datetime.now(timezone.utc) + timedelta(minutes=70)).isoformat())
    ids = {t["title"]: t["id"] for t in state["tasks"]}
    client.post(f"/api/tasks/{ids['calm one']}/move",
                json={"target_id": ids["urgent"], "mode": "before"})
    assert roots(client.get("/api/state").json()) == ["calm one", "urgent"]
    client.put("/api/settings", json={"manual_order": False})
    assert roots(client.get("/api/state").json()) == ["urgent", "calm one"]


def test_manual_order_drives_focus_and_next(client):
    state = create(client, title="second", impact=8, effort=2, estimated_time=10)
    state = create(client, title="first", impact=2, effort=8, estimated_time=10)
    ids = {t["title"]: t["id"] for t in state["tasks"]}
    client.post(f"/api/tasks/{ids['first']}/move",
                json={"target_id": ids["second"], "mode": "before"})
    assert client.get("/api/next").json()["task"]["title"] == "first"
    assert client.get("/api/focus").json()["root_title"] == "first"


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


def test_focus_endpoint_walks_the_tree_depth_first(client):
    state = create(client, title="project")
    project = find(state, "project")
    state = client.post(f"/api/tasks/{project['id']}/breakdown", json={}).json()
    step2 = find(state, "step 2")
    # break the middle step down again -> grandchildren
    client.post(f"/api/tasks/{step2['id']}/breakdown", json={})

    data = client.get("/api/focus").json()
    assert data["root_id"] == project["id"]
    assert data["root_title"] == "project"
    ids = [t["id"] for t in data["queue"]]
    depths = [len(t["path"]) for t in data["queue"]]
    # grandchildren, then the step that contains them, then the last sibling,
    # and the project itself only once everything inside it is behind us
    assert depths == [1, 2, 2, 2, 1, 1, 0]
    assert ids[-1] == project["id"]
    assert ids.index(step2["id"]) == 4
    assert all(t["path"][:2] == ["project", "step 2"]
               for t in data["queue"] if len(t["path"]) == 2)


def test_focus_endpoint_scoped_to_a_root(client):
    state = create(client, title="project")
    project = find(state, "project")
    state = client.post(f"/api/tasks/{project['id']}/breakdown", json={}).json()
    other = create(client, title="unrelated")
    step1 = find(state, "step 1")

    data = client.get(f"/api/focus?root={step1['id']}").json()
    assert [t["title"] for t in data["queue"]] == ["step 1"]
    assert find(other, "unrelated") is not None  # untouched


def test_focus_endpoint_skips_finished_work(client):
    state = create(client, title="project")
    project = find(state, "project")
    state = client.post(f"/api/tasks/{project['id']}/breakdown", json={}).json()
    step1 = find(state, "step 1")
    client.post(f"/api/tasks/{step1['id']}/complete", json={})

    data = client.get(f"/api/focus?root={project['id']}").json()
    assert "step 1" not in [t["title"] for t in data["queue"]]


def test_focus_endpoint_empty_and_unknown_root(client):
    assert client.get("/api/focus").json() == {
        "root_id": None, "root_title": None, "queue": []}
    assert client.get("/api/focus?root=nope").status_code == 404


def test_state_exposes_subtree_rollups(client):
    state = create(client, title="project")
    project = find(state, "project")
    state = client.post(f"/api/tasks/{project['id']}/breakdown", json={}).json()
    project = find(state, "project")
    assert project["has_subtasks"] is True
    # the stub estimates every task at 30 raw minutes -> 39 buffered each
    assert project["rollup_estimate"] == 39 * 3
    assert project["rollup_done"] == 0
    assert project["rollup_remaining"] == 39 * 3
    assert project["rollup_deadline"] is not None

    step1 = find(state, "step 1")
    state = client.post(f"/api/tasks/{step1['id']}/complete", json={}).json()
    project = find(state, "project")
    assert project["rollup_done"] == 39
    assert project["rollup_remaining"] == 39 * 2


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
