"""API tests against a temp database, with the AI layer stubbed out."""

import importlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import logic


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    from app import db
    importlib.reload(db)
    from app import ai, logic, main
    importlib.reload(main)

    # Stub the AI so tests are hermetic.
    # Start times come back only when they were asked for, and two hours out,
    # so a stubbed one is always in the future rather than instantly overdue.
    monkeypatch.setattr(main.ai, "annotate",
                        lambda settings, tasks, want_scores=True,
                               want_start=False, now_local="": {
                            t["id"]: {"id": t["id"], "minutes": 30, "impact": 7,
                                      "effort": 3,
                                      **({"start_in_minutes": 120} if want_start else {})}
                            for t in tasks
                        })
    # AI start times are on for real users and off by default here, so the
    # scheduling tests below go on exercising the quadrant horizon rather than
    # a stub that would pin every task in the suite to the same instant. The
    # tests that are about start times turn the setting back on themselves.
    db.update_settings({"ai_start_times": False})
    monkeypatch.setattr(main.ai, "breakdown",
                        lambda settings, title, desc, granularity, parents=None:
                        [f"step {i}" for i in range(1, 4)])
    # "call dentist" deliberately omits `subtasks` entirely: a leaf is
    # allowed to arrive without the key.
    monkeypatch.setattr(main.ai, "compile_braindump",
                        lambda settings, text: [
                            {"title": "call dentist", "description": ""},
                            {"title": "buy gift", "description": "for mom",
                             "subtasks": [
                                 {"title": "pick a present", "description": "",
                                  "subtasks": [
                                      {"title": "ask her sister", "description": ""},
                                  ]},
                                 {"title": "wrap it", "description": "", "subtasks": []},
                             ]},
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


def test_delete_takes_the_whole_subtree(client):
    """What the list warns about before deleting a container: the nesting goes
    too, at every depth, whether or not a step was already finished."""
    state = create(client, title="move house")
    parent = find(state, "move house")["id"]
    state = create(client, title="book a van", parent_id=parent)
    child = find(state, "book a van")["id"]
    state = create(client, title="compare quotes", parent_id=child)
    grandchild = find(state, "compare quotes")["id"]
    client.patch(f"/api/tasks/{grandchild}", json={"status": "done"})

    state = client.delete(f"/api/tasks/{parent}").json()
    assert state["tasks"] == []
    for title in ("move house", "book a van", "compare quotes"):
        assert find(state, title) is None
    # Gone from the database, not merely absent from the tree.
    assert client.delete(f"/api/tasks/{grandchild}").status_code == 404


def test_delete_subtask_leaves_its_parent_alone(client):
    state = create(client, title="parent")
    parent = find(state, "parent")["id"]
    create(client, title="step one", parent_id=parent)
    state = create(client, title="step two", parent_id=parent)
    step_one = find(state, "step one")["id"]

    state = client.delete(f"/api/tasks/{step_one}").json()
    kept = find(state, "parent")
    assert kept is not None
    assert [s["title"] for s in kept["subtasks"]] == ["step two"]


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


# ---- folding (collapsible tasks) ----

def test_tasks_start_unfolded_and_remember_being_folded(client):
    state = create(client, title="parent")
    pid = find(state, "parent")["id"]
    assert find(state, "parent")["collapsed"] is False

    state = client.patch(f"/api/tasks/{pid}", json={"collapsed": True}).json()
    assert find(state, "parent")["collapsed"] is True
    # survives the round trip: the shape you left the list in is what comes back
    assert find(client.get("/api/state").json(), "parent")["collapsed"] is True

    state = client.patch(f"/api/tasks/{pid}", json={"collapsed": False}).json()
    assert find(state, "parent")["collapsed"] is False


def test_folding_a_task_keeps_its_subtasks(client):
    """Folding is a view state, not a deletion — the subtree is untouched."""
    state = create(client, title="parent")
    pid = find(state, "parent")["id"]
    state = client.post(f"/api/tasks/{pid}/breakdown", json={}).json()
    state = client.patch(f"/api/tasks/{pid}", json={"collapsed": True}).json()
    parent = find(state, "parent")
    assert parent["collapsed"] is True
    assert [s["title"] for s in parent["subtasks"]] == ["step 1", "step 2", "step 3"]
    assert parent["has_subtasks"] is True
    assert parent["rollup_estimate"] is not None


def test_adding_a_subtask_unfolds_the_parent(client):
    state = create(client, title="parent")
    pid = find(state, "parent")["id"]
    client.patch(f"/api/tasks/{pid}", json={"collapsed": True})
    state = create(client, title="by hand", parent_id=pid)
    assert find(state, "parent")["collapsed"] is False


def test_breakdown_unfolds_the_task(client):
    state = create(client, title="parent")
    pid = find(state, "parent")["id"]
    client.patch(f"/api/tasks/{pid}", json={"collapsed": True})
    state = client.post(f"/api/tasks/{pid}/breakdown", json={}).json()
    assert find(state, "parent")["collapsed"] is False


def test_dropping_a_task_into_a_folded_one_unfolds_it(client):
    state = create(client, title="a")
    state = create(client, title="b")
    ids = {t["title"]: t["id"] for t in state["tasks"]}
    client.patch(f"/api/tasks/{ids['a']}", json={"collapsed": True})
    state = client.post(f"/api/tasks/{ids['b']}/move",
                        json={"target_id": ids["a"], "mode": "into"}).json()
    a = find(state, "a")
    assert a["collapsed"] is False
    assert [s["title"] for s in a["subtasks"]] == ["b"]


def test_reordering_beside_a_folded_task_leaves_it_folded(client):
    """Only landing *inside* a folded task opens it."""
    state = create(client, title="a")
    state = create(client, title="b")
    ids = {t["title"]: t["id"] for t in state["tasks"]}
    client.patch(f"/api/tasks/{ids['a']}", json={"collapsed": True})
    state = client.post(f"/api/tasks/{ids['b']}/move",
                        json={"target_id": ids["a"], "mode": "before"}).json()
    assert find(state, "a")["collapsed"] is True


def roots(state):
    """Top-level titles in the order the page shows them (see sortActive)."""
    return [t["title"] for t in sorted(state["tasks"],
                                       key=lambda t: t["list_sort_key"])]


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


# ---- the sorter ----

def _dated(client, title, **when):
    return create(client, title=title,
                  deadline=(datetime.now(timezone.utc) + timedelta(**when)).isoformat())


def test_sorting_by_deadline_reorders_the_list(client):
    _dated(client, "middle", days=2)
    _dated(client, "last", days=5)
    _dated(client, "first", hours=2)

    client.put("/api/settings", json={"sort_field": "deadline", "sort_dir": "asc"})
    assert roots(client.get("/api/state").json()) == ["first", "middle", "last"]
    client.put("/api/settings", json={"sort_field": "deadline", "sort_dir": "desc"})
    assert roots(client.get("/api/state").json()) == ["last", "middle", "first"]


def test_sorting_by_subtasks_reorders_the_list(client):
    create(client, title="flat")
    state = create(client, title="project")
    pid = find(state, "project")["id"]
    client.post(f"/api/tasks/{pid}/breakdown", json={})

    client.put("/api/settings", json={"sort_field": "subtasks", "sort_dir": "desc"})
    state = client.get("/api/state").json()
    assert roots(state) == ["project", "flat"]
    assert find(state, "project")["open_subtasks"] == 3
    client.put("/api/settings", json={"sort_field": "subtasks", "sort_dir": "asc"})
    assert roots(client.get("/api/state").json()) == ["flat", "project"]


def test_sorting_leaves_what_next_alone(client):
    """Reading the list a different way is not a change of plan."""
    create(client, title="calm", impact=8, effort=2, estimated_time=10)
    state = create(client, title="urgent", estimated_time=60,
                   deadline=(datetime.now(timezone.utc) + timedelta(minutes=70)).isoformat())
    urgent_id = find(state, "urgent")["id"]

    client.put("/api/settings", json={"sort_field": "created", "sort_dir": "asc"})
    state = client.get("/api/state").json()
    assert roots(state)[0] == "calm"
    assert state["next_task_id"] == urgent_id
    assert client.get("/api/next").json()["task"]["title"] == "urgent"


def test_dragging_while_sorted_keeps_the_order_you_were_looking_at(client):
    _dated(client, "a", days=1)
    _dated(client, "b", days=2)
    _dated(client, "c", days=3)
    client.put("/api/settings", json={"sort_field": "deadline", "sort_dir": "desc"})
    state = client.get("/api/state").json()
    assert roots(state) == ["c", "b", "a"]

    ids = {t["title"]: t["id"] for t in state["tasks"]}
    state = client.post(f"/api/tasks/{ids['a']}/move",
                        json={"target_id": ids["c"], "mode": "before"}).json()
    # the drag froze the deadline order that was on screen and moved one task
    assert roots(state) == ["a", "c", "b"]
    settings = client.get("/api/settings").json()
    assert settings["sort_field"] == "manual"
    assert settings["manual_order"] is True


def test_the_sorter_and_the_manual_order_checkbox_are_one_switch(client):
    assert client.get("/api/settings").json()["sort_field"] == "smart"
    assert client.put("/api/settings",
                      json={"manual_order": True}).json()["sort_field"] == "manual"
    assert client.put("/api/settings",
                      json={"manual_order": False}).json()["sort_field"] == "smart"
    got = client.put("/api/settings", json={"sort_field": "score"}).json()
    assert got["manual_order"] is False
    got = client.put("/api/settings", json={"sort_field": "manual"}).json()
    assert got["manual_order"] is True


def test_saving_the_settings_modal_leaves_the_sorter_alone(client):
    """The modal sends the whole form every time, checkbox included."""
    client.put("/api/settings", json={"sort_field": "score", "sort_dir": "asc"})
    got = client.put("/api/settings",
                     json={"manual_order": False, "granularity": 4}).json()
    assert got["sort_field"] == "score" and got["sort_dir"] == "asc"


def test_unknown_sort_choices_fall_back_to_smart(client):
    got = client.put("/api/settings",
                     json={"sort_field": "vibes", "sort_dir": "sideways"}).json()
    assert got["sort_field"] == "smart" and got["sort_dir"] == "desc"


def test_compile_braindump(client):
    res = client.post("/api/compile", json={"text": "dentist... mom bday..."})
    assert res.status_code == 200
    state = res.json()
    assert find(state, "call dentist")
    assert find(state, "buy gift")["description"] == "for mom"


def test_compile_braindump_nests_tasks(client):
    state = client.post("/api/compile", json={"text": "dentist... mom bday..."}).json()
    # Only the two top-level items are roots; the rest hang off "buy gift".
    assert [t["title"] for t in state["tasks"]] == ["call dentist", "buy gift"]
    gift = find(state, "buy gift")
    assert [t["title"] for t in gift["subtasks"]] == ["pick a present", "wrap it"]
    present = find(state, "pick a present")
    assert present["parent_id"] == gift["id"]
    # ...and nesting goes deeper than one level.
    assert [t["title"] for t in present["subtasks"]] == ["ask her sister"]
    assert find(state, "ask her sister")["parent_id"] == present["id"]
    # A compiled subtask lives in the same project as the root it came under.
    assert find(state, "ask her sister")["project_id"] == gift["project_id"]


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
    empty = client.get("/api/focus").json()
    assert empty["root_id"] is None and empty["root_title"] is None
    assert empty["queue"] == []
    assert empty["project_id"]  # an empty tab is still a tab
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


# ---------------- projects (tabs) ----------------

def new_project(client, name="side quests"):
    res = client.post("/api/projects", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


def test_default_project_exists_and_owns_new_tasks(client):
    state = client.get("/api/state").json()
    assert len(state["projects"]) == 1
    project = state["projects"][0]
    assert project["name"] == "Tasks"
    assert state["active_project_id"] == project["id"]
    task = find(create(client, title="wash dishes"), "wash dishes")
    assert task["project_id"] == project["id"]


def test_creating_a_project_switches_to_it_and_scopes_tasks(client):
    home = find(create(client, title="home task"), "home task")
    state = new_project(client, "work")
    work = state["active_project_id"]
    assert work != home["project_id"]
    assert state["tasks"] == []  # a new tab starts empty

    state = create(client, title="work task")
    assert find(state, "work task")["project_id"] == work
    assert find(state, "home task") is None  # the other tab is not on screen

    # Switching back shows exactly the other list.
    state = client.post(f"/api/projects/{home['project_id']}/activate").json()
    assert find(state, "home task") is not None
    assert find(state, "work task") is None


def test_active_project_survives_a_restart(client):
    state = new_project(client, "work")
    work = state["active_project_id"]
    assert client.get("/api/state").json()["active_project_id"] == work


def test_tab_counts_only_unfinished_tasks(client):
    state = create(client, title="a")
    create(client, title="b")
    state = client.post(f"/api/tasks/{find(state, 'a')['id']}/complete", json={}).json()
    assert state["projects"][0]["open_tasks"] == 1


def test_subtasks_and_braindump_land_in_the_open_tab(client):
    state = new_project(client, "work")
    work = state["active_project_id"]
    parent = find(create(client, title="clean kitchen"), "clean kitchen")
    state = client.post(f"/api/tasks/{parent['id']}/breakdown", json={}).json()
    assert all(s["project_id"] == work for s in find(state, "clean kitchen")["subtasks"])
    state = client.post("/api/compile", json={"text": "stuff"}).json()
    assert find(state, "call dentist")["project_id"] == work


def test_rename_project(client):
    state = new_project(client, "work")
    pid = state["active_project_id"]
    state = client.patch(f"/api/projects/{pid}", json={"name": "deep work"}).json()
    assert [p["name"] for p in state["projects"]] == ["Tasks", "deep work"]


def test_project_name_cannot_be_blanked(client):
    pid = client.get("/api/state").json()["active_project_id"]
    assert client.patch(f"/api/projects/{pid}", json={"name": "   "}).status_code == 400
    assert client.get("/api/state").json()["projects"][0]["name"] == "Tasks"


def test_delete_project_removes_its_tasks_and_falls_back(client):
    first = client.get("/api/state").json()["active_project_id"]
    state = new_project(client, "work")
    work = state["active_project_id"]
    doomed = find(create(client, title="work task"), "work task")["id"]

    state = client.delete(f"/api/projects/{work}").json()
    assert state["active_project_id"] == first
    assert [p["id"] for p in state["projects"]] == [first]
    assert client.get(f"/api/state").json()["tasks"] == []
    # the tasks went with it
    from app import db
    assert db.get_task(doomed) is None


def test_cannot_delete_the_only_project(client):
    pid = client.get("/api/state").json()["active_project_id"]
    res = client.delete(f"/api/projects/{pid}")
    assert res.status_code == 400
    assert client.get("/api/state").json()["projects"]


def test_unknown_project_is_404(client):
    assert client.post("/api/projects/nope/activate").status_code == 404
    assert client.patch("/api/projects/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/projects/nope").status_code == 404


def names(state):
    return [p["name"] for p in state["projects"]]


def three_projects(client):
    """The default tab plus two more: "Tasks", "work", "errands"."""
    new_project(client, "work")
    new_project(client, "errands")
    return client.get("/api/state").json()["projects"]


def test_projects_start_in_the_order_they_were_made(client):
    three_projects(client)
    assert names(client.get("/api/state").json()) == ["Tasks", "work", "errands"]


def test_drag_a_tab_before_another_one(client):
    projects = three_projects(client)
    errands = projects[2]["id"]
    state = client.post(f"/api/projects/{errands}/move",
                        json={"target_id": projects[0]["id"], "mode": "before"}).json()
    assert names(state) == ["errands", "Tasks", "work"]


def test_drag_a_tab_after_another_one(client):
    projects = three_projects(client)
    tasks = projects[0]["id"]
    state = client.post(f"/api/projects/{tasks}/move",
                        json={"target_id": projects[2]["id"], "mode": "after"}).json()
    assert names(state) == ["work", "errands", "Tasks"]


def test_dropping_a_tab_past_the_last_one_sends_it_to_the_end(client):
    projects = three_projects(client)
    state = client.post(f"/api/projects/{projects[0]['id']}/move", json={}).json()
    assert names(state) == ["work", "errands", "Tasks"]


def test_move_a_tab_to_an_explicit_position(client):
    projects = three_projects(client)
    state = client.post(f"/api/projects/{projects[2]['id']}/move",
                        json={"position": 1}).json()
    assert names(state) == ["Tasks", "errands", "work"]
    # Past the end of the strip is the end of the strip, not an error.
    state = client.post(f"/api/projects/{projects[0]['id']}/move",
                        json={"position": 99}).json()
    assert names(state) == ["errands", "work", "Tasks"]


def test_reordering_tabs_leaves_the_open_one_open(client):
    projects = three_projects(client)
    work = projects[1]["id"]
    client.post(f"/api/projects/{work}/activate")
    task = find(create(client, title="work task"), "work task")
    state = client.post(f"/api/projects/{work}/move",
                        json={"target_id": projects[0]["id"], "mode": "before"}).json()
    assert names(state) == ["work", "Tasks", "errands"]
    assert state["active_project_id"] == work
    assert find(state, "work task")["id"] == task["id"]


def test_a_new_tab_lands_at_the_end_of_a_reordered_strip(client):
    projects = three_projects(client)
    client.post(f"/api/projects/{projects[2]['id']}/move",
                json={"target_id": projects[0]["id"], "mode": "before"})
    state = new_project(client, "later")
    assert names(state) == ["errands", "Tasks", "work", "later"]


def test_tab_order_survives_a_restart(client):
    projects = three_projects(client)
    client.post(f"/api/projects/{projects[2]['id']}/move",
                json={"target_id": projects[0]["id"], "mode": "before"})
    assert names(client.get("/api/state").json()) == ["errands", "Tasks", "work"]


def test_deleting_a_tab_leaves_the_rest_in_order(client):
    projects = three_projects(client)
    state = client.delete(f"/api/projects/{projects[1]['id']}").json()
    assert names(state) == ["Tasks", "errands"]


def test_bad_tab_moves_are_refused(client):
    projects = three_projects(client)
    tasks, work = projects[0]["id"], projects[1]["id"]
    assert client.post("/api/projects/nope/move", json={}).status_code == 404
    assert client.post(f"/api/projects/{tasks}/move",
                       json={"target_id": "nope", "mode": "before"}).status_code == 404
    assert client.post(f"/api/projects/{tasks}/move",
                       json={"target_id": tasks, "mode": "before"}).status_code == 400
    assert client.post(f"/api/projects/{tasks}/move",
                       json={"target_id": work}).status_code == 400
    assert client.post(f"/api/projects/{tasks}/move",
                       json={"target_id": work, "mode": "sideways"}).status_code == 422
    # nothing moved
    assert names(client.get("/api/state").json()) == ["Tasks", "work", "errands"]


def test_move_task_to_another_project_takes_its_subtasks(client):
    parent = find(create(client, title="clean kitchen"), "clean kitchen")
    client.post(f"/api/tasks/{parent['id']}/breakdown", json={})
    state = new_project(client, "chores")
    chores = state["active_project_id"]

    state = client.post(f"/api/tasks/{parent['id']}/project",
                        json={"project_id": chores}).json()
    moved = find(state, "clean kitchen")
    assert moved["project_id"] == chores
    assert moved["parent_id"] is None
    assert [s["project_id"] for s in moved["subtasks"]] == [chores] * 3


def test_move_task_to_unknown_project_is_404(client):
    tid = find(create(client, title="x"), "x")["id"]
    assert client.post(f"/api/tasks/{tid}/project",
                       json={"project_id": "nope"}).status_code == 404


def test_tasks_cannot_be_dragged_across_projects(client):
    home = find(create(client, title="home task"), "home task")
    new_project(client, "work")
    work_task = find(create(client, title="work task"), "work task")
    res = client.post(f"/api/tasks/{work_task['id']}/move",
                      json={"target_id": home["id"], "mode": "into"})
    assert res.status_code == 400
    res = client.post(f"/api/tasks/{work_task['id']}/move",
                      json={"parent_id": home["id"]})
    assert res.status_code == 400


def test_ordering_is_independent_per_project(client):
    a = find(create(client, title="a"), "a")
    b = find(create(client, title="b"), "b")
    new_project(client, "work")
    x = find(create(client, title="x"), "x")
    y = find(create(client, title="y"), "y")

    # Dragging in one tab freezes every tab's order but reshuffles neither.
    state = client.post(f"/api/tasks/{y['id']}/move",
                        json={"target_id": x["id"], "mode": "before"}).json()
    assert [t["title"] for t in state["tasks"]] == ["y", "x"]
    state = client.post(f"/api/projects/{a['project_id']}/activate").json()
    assert sorted(t["title"] for t in state["tasks"]) == ["a", "b"]
    assert {t["id"] for t in state["tasks"]} == {a["id"], b["id"]}


def test_focus_stays_in_the_project_of_its_root(client):
    home = find(create(client, title="home task"), "home task")
    new_project(client, "work")
    create(client, title="work task")

    # The open tab decides an unscoped session…
    assert client.get("/api/focus").json()["queue"][0]["title"] == "work task"
    # …but a session already running in another tab follows its own root.
    data = client.get(f"/api/focus?root={home['id']}").json()
    assert data["project_id"] == home["project_id"]
    assert [t["title"] for t in data["queue"]] == ["home task"]


def test_alarm_tasks_span_every_project(client):
    due = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    create(client, title="home task", deadline=due)
    new_project(client, "work")
    state = create(client, title="work task", deadline=due)
    titles = {t["title"] for t in state["alarm_tasks"]}
    assert {"home task", "work task"} <= titles
    assert {t["project_name"] for t in state["alarm_tasks"]} == {"Tasks", "work"}


def test_next_task_ignores_other_projects(client):
    create(client, title="home task")
    state = new_project(client, "work")
    assert state["next_task_id"] is None
    state = create(client, title="work task")
    assert state["next_task_id"] == find(state, "work task")["id"]


def test_pre_projects_database_is_migrated(tmp_path, monkeypatch):
    """An existing install upgrades in place: its tasks become the first tab."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            parent_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
            deadline TEXT, estimated_time INTEGER, actual_time INTEGER,
            impact INTEGER, effort INTEGER,
            status TEXT NOT NULL DEFAULT 'todo',
            ack_thankless INTEGER NOT NULL DEFAULT 0,
            order_index INTEGER NOT NULL DEFAULT 0, started_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        INSERT INTO tasks (id, title, order_index, created_at, updated_at)
        VALUES ('old1', 'from before', 0, '2024-01-01T00:00:00+00:00',
                '2024-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("ADDERALL_DB", str(path))
    from app import db
    importlib.reload(db)
    from app import main
    importlib.reload(main)
    client = TestClient(main.app)

    state = client.get("/api/state").json()
    assert len(state["projects"]) == 1
    assert [t["title"] for t in state["tasks"]] == ["from before"]
    assert state["tasks"][0]["project_id"] == state["projects"][0]["id"]
    # the folding column is added too, and everything starts open — exactly
    # how the list looked before it existed
    assert state["tasks"][0]["collapsed"] is False
    # and running init again changes nothing
    db.init()
    assert len(db.list_projects()) == 1


# ---- calendar ----

def iso_in(**kw):
    """A deadline the front end could have sent — whole seconds, like the app."""
    return (datetime.now(timezone.utc) + timedelta(**kw)).replace(
        microsecond=0).isoformat()


def event(payload, title):
    return next(e for e in payload["events"] if e["title"] == title)


def test_calendar_spans_every_project(client):
    """The calendar is the one view that ignores which tab is open."""
    create(client, title="in the first tab", deadline=iso_in(days=1))
    other = client.post("/api/projects", json={"name": "House"}).json()
    assert other["active_project_id"] != client.get("/api/state").json()["projects"][0]["id"]
    create(client, title="in the second tab", deadline=iso_in(days=2))

    payload = client.get("/api/calendar").json()
    titles = {e["title"] for e in payload["events"]}
    assert {"in the first tab", "in the second tab"} <= titles
    assert {e["project_name"] for e in payload["events"]} == {"Tasks", "House"}


def test_calendar_events_carry_a_score_and_a_length(client):
    create(client, title="urgent", deadline=iso_in(minutes=20),
           estimated_time=60, impact=9, effort=2)
    create(client, title="relaxed", deadline=iso_in(days=20),
           estimated_time=60, impact=2, effort=9)
    payload = client.get("/api/calendar").json()

    urgent, relaxed = event(payload, "urgent"), event(payload, "relaxed")
    assert urgent["score"] > relaxed["score"]
    # A block is one buffered estimate long, and says how much of that is tax.
    assert urgent["length_min"] == 78          # 60 * 1.3
    assert urgent["raw_length_min"] == 60
    assert urgent["quadrant"] == "quick_win"


def test_calendar_length_falls_back_when_nothing_is_estimated(client):
    create(client, title="unknown", deadline=iso_in(days=1),
           estimated_time=None, annotate=False)
    ev = event(client.get("/api/calendar").json(), "unknown")
    assert ev["length_min"] == 30              # the same default urgency uses
    assert ev["score"] is not None


def test_calendar_container_is_worth_the_work_it_still_holds(client):
    state = create(client, title="big thing", deadline=iso_in(days=3))
    parent = find(state, "big thing")
    client.post("/api/tasks", json={"title": "step", "parent_id": parent["id"],
                                    "estimated_time": 100})
    ev = event(client.get("/api/calendar").json(), "big thing")
    assert ev["has_subtasks"] is True
    assert ev["subtask_count"] == 1
    assert ev["length_min"] == 130             # the subtask, buffered


def test_calendar_keeps_done_work_but_drops_discarded(client):
    state = create(client, title="finished", deadline=iso_in(hours=1))
    client.post(f"/api/tasks/{find(state, 'finished')['id']}/complete", json={})
    state = create(client, title="dropped", deadline=iso_in(hours=1))
    client.patch(f"/api/tasks/{find(state, 'dropped')['id']}",
                 json={"status": "discarded"})

    titles = {e["title"] for e in client.get("/api/calendar").json()["events"]}
    assert "finished" in titles       # a calendar you can look back at
    assert "dropped" not in titles    # discarded work is off the plan


def test_calendar_omits_tasks_with_no_date_at_all(client):
    client.put("/api/settings", json={"auto_deadlines": False})
    create(client, title="someday")
    assert client.get("/api/calendar").json()["events"] == []


def test_calendar_reports_where_a_deadline_came_from(client):
    create(client, title="mine", deadline=iso_in(days=1))
    create(client, title="theirs")
    payload = client.get("/api/calendar").json()
    assert event(payload, "mine")["deadline_source"] == "user"
    assert event(payload, "theirs")["deadline_source"] == "auto"


def test_calendar_event_carries_its_ancestry(client):
    state = create(client, title="project")
    parent = find(state, "project")
    client.post("/api/tasks", json={"title": "step", "parent_id": parent["id"]})
    ev = event(client.get("/api/calendar").json(), "step")
    assert ev["path"] == ["project"]
    assert ev["parent_id"] == parent["id"]


# ---- nudging past-due work ----

def test_nudge_moves_a_past_due_deadline_and_keeps_the_length(client):
    state = create(client, title="overdue", deadline=iso_in(days=-2),
                   estimated_time=60)
    task = find(state, "overdue")
    before = event(client.get("/api/calendar").json(), "overdue")

    target = datetime.now(timezone.utc) + timedelta(days=1)
    res = client.post("/api/nudge", json={
        "nudges": [{"task_id": task["id"], "deadline": target.isoformat()}]})
    assert res.status_code == 200, res.text

    after = event(client.get("/api/calendar").json(), "overdue")
    assert logic_parse(after["deadline"]) == target.replace(microsecond=0)
    assert after["deadline_source"] == "user"
    # "Same length" is the promise: the block is the size it always was.
    assert after["length_min"] == before["length_min"] == 78


def logic_parse(iso):
    from app import logic
    return logic.parse_dt(iso)


def test_nudge_slides_subtask_deadlines_by_the_same_amount(client):
    state = create(client, title="trip", deadline=iso_in(days=-1))
    parent = find(state, "trip")
    state = client.post("/api/tasks", json={
        "title": "pack", "parent_id": parent["id"],
        "deadline": iso_in(days=-3)}).json()
    child = find(state, "pack")

    gap_before = (logic_parse(parent["deadline"]) - logic_parse(child["deadline"]))
    target = datetime.now(timezone.utc) + timedelta(days=4)
    client.post("/api/nudge", json={
        "nudges": [{"task_id": parent["id"], "deadline": target.isoformat()}]})

    payload = client.get("/api/calendar").json()
    gap_after = (logic_parse(event(payload, "trip")["deadline"])
                 - logic_parse(event(payload, "pack")["deadline"]))
    assert gap_after == gap_before          # the plan kept its shape
    assert logic_parse(event(payload, "pack")["deadline"]) > datetime.now(timezone.utc)


def test_nudge_moves_the_whole_overdue_pile_in_one_call(client):
    ids = []
    for title in ("one", "two", "three"):
        state = create(client, title=title, deadline=iso_in(days=-1))
        ids.append(find(state, title)["id"])
    target = datetime.now(timezone.utc) + timedelta(days=2)
    client.post("/api/nudge", json={
        "nudges": [{"task_id": i, "deadline": target.isoformat()} for i in ids]})

    payload = client.get("/api/calendar").json()
    now = datetime.now(timezone.utc)
    assert all(logic_parse(e["deadline"]) > now for e in payload["events"])


def test_nudge_an_explicitly_named_subtask_wins_over_the_slide(client):
    state = create(client, title="trip", deadline=iso_in(days=-1))
    parent = find(state, "trip")
    state = client.post("/api/tasks", json={
        "title": "pack", "parent_id": parent["id"],
        "deadline": iso_in(days=-3)}).json()
    child = find(state, "pack")

    parent_target = datetime.now(timezone.utc) + timedelta(days=5)
    child_target = datetime.now(timezone.utc) + timedelta(days=1)
    client.post("/api/nudge", json={"nudges": [
        {"task_id": parent["id"], "deadline": parent_target.isoformat()},
        {"task_id": child["id"], "deadline": child_target.isoformat()},
    ]})
    payload = client.get("/api/calendar").json()
    assert logic_parse(event(payload, "pack")["deadline"]) == \
        child_target.replace(microsecond=0)


def test_nudge_rejects_unknown_tasks_and_unparseable_dates(client):
    state = create(client, title="real", deadline=iso_in(days=-1))
    task = find(state, "real")
    assert client.post("/api/nudge", json={
        "nudges": [{"task_id": "nope", "deadline": iso_in(days=1)}]}
    ).status_code == 404
    assert client.post("/api/nudge", json={
        "nudges": [{"task_id": task["id"], "deadline": "next tuesday"}]}
    ).status_code == 400
    assert client.post("/api/nudge", json={"nudges": []}).status_code == 422
    # Nothing was written by any of the rejected calls.
    assert find(client.get("/api/state").json(), "real")["deadline_source"] == "user"


def test_week_start_setting_round_trips(client):
    assert client.get("/api/settings").json()["week_start"] == 0
    assert client.put("/api/settings", json={"week_start": 1}).json()["week_start"] == 1


def test_state_carries_the_length_a_nudge_preserves(client):
    """The list offers the same nudge the calendar does, so it needs the same
    number: how long the task is, so the dialog can promise to keep it."""
    state = create(client, title="overdue thing", deadline=iso_in(days=-1),
                   estimated_time=60)
    task = find(state, "overdue thing")
    assert task["length_min"] == 78
    assert task["raw_length_min"] == 60
    # ...and nudging it from there leaves that length alone.
    target = datetime.now(timezone.utc) + timedelta(days=1)
    state = client.post("/api/nudge", json={
        "nudges": [{"task_id": task["id"], "deadline": target.isoformat()}]}).json()
    assert find(state, "overdue thing")["length_min"] == 78


# ---- spreading auto-deadlines over days that have room ----

def blocks(events):
    """(start, end) of every open event's calendar block, in time order."""
    out = []
    for e in events:
        if e["status"] == "done":
            continue
        end = datetime.fromisoformat(e["deadline"])
        out.append((end - timedelta(minutes=e["length_min"]), end))
    return sorted(out)


def test_auto_deadlines_do_not_stack_on_one_instant(client):
    """The braindump case, end to end: alike tasks, created in one breath."""
    for i in range(4):
        create(client, title=f"chunk {i}", estimated_time=120, impact=8, effort=2)
    events = client.get("/api/calendar").json()["events"]
    assert len({e["deadline"] for e in events}) == 4  # not one shared instant

    placed = blocks(events)
    for (_, first_end), (second_start, _) in zip(placed, placed[1:]):
        assert second_start >= first_end          # nothing sits on anything else
    # 156 buffered minutes each: three fill an eight-hour day and the fourth
    # rolls onto the next one instead of overflowing it.
    assert len({start.date() for start, _ in placed}) == 2


def test_auto_deadlines_plan_around_deadlines_you_set(client):
    create(client, title="dentist", estimated_time=240,
           deadline=iso_in(days=1), impact=8, effort=2)
    create(client, title="placed around it", estimated_time=120,
           impact=8, effort=2)
    payload = client.get("/api/calendar").json()
    assert event(payload, "dentist")["deadline_source"] == "user"
    # The commitment keeps its slot; the app's own work goes somewhere else.
    placed = blocks(payload["events"])
    for (_, first_end), (second_start, _) in zip(placed, placed[1:]):
        assert second_start >= first_end


def test_auto_deadlines_span_projects(client):
    """One day, two tabs: the second tab plans around the first."""
    create(client, title="work thing", estimated_time=300, impact=8, effort=2)
    client.post("/api/projects", json={"name": "House"})
    create(client, title="house thing", estimated_time=300, impact=8, effort=2)
    placed = blocks(client.get("/api/calendar").json()["events"])
    # 390 buffered minutes each — they cannot share an eight-hour day.
    assert len(placed) == 2
    assert placed[0][1] <= placed[1][0]
    assert placed[0][0].date() != placed[1][0].date()


def test_spreading_can_be_turned_off(client):
    client.put("/api/settings", json={"spread_tasks": False})
    for i in range(3):
        create(client, title=f"chunk {i}", estimated_time=120, impact=8, effort=2)
    events = client.get("/api/calendar").json()["events"]
    # Back to the plain horizon: created plus a day, with nothing pushed onto a
    # later day to make room. Deadlines can differ by the second each task was
    # created in — that is the horizon doing exactly its job — so the assertion
    # is that they are all the same moment, not that they are the same string.
    stamps = sorted(logic.parse_dt(e["deadline"]) for e in events)
    assert (stamps[-1] - stamps[0]).total_seconds() < 60


# ---- the daily cap ----

def test_calendar_carries_the_day_cap(client):
    cap = client.get("/api/calendar").json()["capacity"]
    assert cap["minutes"] == 480      # eight hours until it learns otherwise
    assert cap["base"] == 480
    assert cap["learned"] is False
    assert cap["days"] == 0


def test_day_cap_learns_from_days_you_actually_finish(client):
    """Six days of three-hour days: the eight-hour goal moves to meet you."""
    from app import db
    for day in range(1, 7):
        task = db.create_task({"title": f"done {day}", "estimated_time": 180,
                               "actual_time": 180, "status": "done",
                               "project_id": db.list_projects()[0]["id"]})
        db.update_task(task["id"], {"status": "done"})
        with db.connect() as conn:
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                         ((datetime.now(timezone.utc) - timedelta(days=day))
                          .isoformat(timespec="seconds"), task["id"]))

    cap = client.get("/api/calendar").json()["capacity"]
    assert cap["days"] == 6
    assert cap["typical"] == 180
    assert cap["hit_rate"] == 0.0
    assert cap["minutes"] == 330       # halfway from 480 to 180
    assert cap["learned"] is True
    # ...and the settings dialog is told the same story.
    assert client.get("/api/settings").json()["capacity"]["minutes"] == 330


def test_day_cap_settings_roundtrip(client):
    res = client.put("/api/settings", json={
        "day_capacity": 300, "adaptive_capacity": False,
        "day_start": 7, "timezone": "America/New_York",
        "capacity": {"minutes": 9999},   # derived: echoed back, never stored
    }).json()
    assert res["day_capacity"] == 300
    assert res["adaptive_capacity"] is False
    assert res["day_start"] == 7
    assert res["timezone"] == "America/New_York"
    assert res["capacity"]["minutes"] == 300


# ---- XP: what finishing something pays out ----

def test_state_carries_the_level_and_xp(client):
    state = client.get("/api/state").json()
    assert state["xp"] == {"total": 0, "level": 1, "into_level": 0,
                           "level_span": 100, "to_next": 100, "progress": 0.0,
                           "gained": 0}


def test_completing_a_task_pays_out_its_score(client):
    state = create(client, title="a real task", impact=9, effort=2,
                   estimated_time=20)
    task = find(state, "a real task")
    score = task["score"]
    assert score is not None
    state = client.post(f"/api/tasks/{task['id']}/complete", json={}).json()
    expected = round(score)
    assert state["xp"]["gained"] == expected
    assert state["xp"]["total"] == expected
    # ...and the task remembers what it paid, for the Done list to show.
    assert find(state, "a real task")["xp_awarded"] == expected


def test_a_task_never_pays_twice(client):
    state = create(client, title="once", impact=5, effort=5)
    tid = find(state, "once")["id"]
    first = client.post(f"/api/tasks/{tid}/complete", json={}).json()["xp"]["total"]
    assert first > 0
    # Reopened and finished again — the XP stands, but it is not paid again.
    client.patch(f"/api/tasks/{tid}", json={"status": "todo"})
    again = client.post(f"/api/tasks/{tid}/complete", json={}).json()["xp"]
    assert again["gained"] == 0
    assert again["total"] == first


def test_a_container_pays_through_its_steps_not_twice(client):
    state = create(client, title="project")
    tid = find(state, "project")["id"]
    state = client.post(f"/api/tasks/{tid}/breakdown", json={}).json()
    steps = find(state, "project")["subtasks"]
    assert len(steps) == 3
    expected = sum(round(s["score"]) for s in steps)
    state = client.post(f"/api/tasks/{tid}/complete", json={}).json()
    # The parent's own score is the mean of the steps; paying for it as well
    # would pay twice for one afternoon.
    assert state["xp"]["gained"] == expected
    assert find(state, "project")["xp_awarded"] is None
    assert all(s["xp_awarded"] for s in find(state, "project")["subtasks"])


def test_finishing_by_patch_pays_the_same_as_the_button(client):
    state = create(client, title="patched", impact=7, effort=3)
    task = find(state, "patched")
    state = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}).json()
    assert state["xp"]["gained"] == round(task["score"])


def test_discarding_pays_nothing(client):
    state = create(client, title="dropped")
    tid = find(state, "dropped")["id"]
    state = client.patch(f"/api/tasks/{tid}", json={"status": "discarded"}).json()
    assert state["xp"]["total"] == 0
    assert find(state, "dropped")["xp_awarded"] is None


def test_xp_survives_deleting_the_task_that_earned_it(client):
    state = create(client, title="gone soon", impact=8, effort=2)
    tid = find(state, "gone soon")["id"]
    earned = client.post(f"/api/tasks/{tid}/complete", json={}).json()["xp"]["total"]
    assert earned > 0
    state = client.delete(f"/api/tasks/{tid}").json()
    assert state["xp"]["total"] == earned


def test_enough_finished_tasks_raise_the_level(client):
    total = 0
    for i in range(6):
        state = create(client, title=f"task {i}", impact=10, effort=0,
                       estimated_time=10)
        tid = find(state, f"task {i}")["id"]
        total = client.post(f"/api/tasks/{tid}/complete", json={}).json()["xp"]["total"]
    xp = client.get("/api/state").json()["xp"]
    assert xp["total"] == total
    assert xp["level"] > 1                     # six real tasks is a level or two
    assert xp["level"] == logic.level_progress(total)["level"]
    assert xp["into_level"] + xp["to_next"] == xp["level_span"]


# ---- start times, end to end ----

def test_a_start_time_you_set_survives_the_round_trip(client):
    # On a whole minute, because the planner books whole local minutes and a
    # start time with seconds on it is rounded up to the next one.
    at = (datetime.now(timezone.utc) + timedelta(hours=5)).replace(
        second=0, microsecond=0)
    when = at.isoformat()
    task = find(create(client, title="eat dinner", start_at=when), "eat dinner")
    assert task["start_at"] == when
    # And it is what the task is scheduled from: the block begins there.
    end = datetime.fromisoformat(task["deadline"])
    assert end - timedelta(minutes=task["length_min"]) == at


def test_a_start_time_can_be_changed_and_cleared(client):
    task = find(create(client, title="thing", start_at=iso_in(hours=2)), "thing")
    moved = iso_in(days=3)
    state = client.patch(f"/api/tasks/{task['id']}", json={"start_at": moved}).json()
    assert find(state, "thing")["start_at"] == moved

    state = client.patch(f"/api/tasks/{task['id']}",
                         json={"clear_start_at": True}).json()
    assert find(state, "thing")["start_at"] is None


def test_a_start_time_weeks_out_sinks_below_work_for_today(client):
    """The two halves of the feature, side by side on one list."""
    create(client, title="eat dinner", start_at=iso_in(hours=3),
           estimated_time=30, impact=5, effort=2)
    create(client, title="finish that game", start_at=iso_in(days=30),
           estimated_time=240, impact=5, effort=2)
    state = client.get("/api/state").json()

    dinner, game = find(state, "eat dinner"), find(state, "finish that game")
    assert dinner["urgency"] > game["urgency"]
    assert dinner["score"] > game["score"]
    assert state["next_task_id"] == dinner["id"]


def test_the_ai_fills_in_a_start_time_for_a_new_top_level_task(client):
    client.put("/api/settings", json={"ai_start_times": True})
    before = datetime.now(timezone.utc)
    task = find(create(client, title="eat dinner"), "eat dinner")
    # The stub answers "in two hours"; the app turns that offset into an instant.
    assert task["start_at"] is not None
    gap = datetime.fromisoformat(task["start_at"]) - before
    assert timedelta(minutes=118) <= gap <= timedelta(minutes=122)


def test_the_ai_leaves_start_times_off_the_steps_inside_a_task(client):
    """A step is scheduled inside its parent's slot, so a start time on one
    would be a number nothing reads."""
    client.put("/api/settings", json={"ai_start_times": True})
    parent = find(create(client, title="move house"), "move house")
    state = client.post(f"/api/tasks/{parent['id']}/breakdown", json={}).json()
    steps = find(state, "move house")["subtasks"]
    assert steps and all(s["start_at"] is None for s in steps)


def test_a_start_time_the_ai_suggested_can_be_turned_off(client):
    client.put("/api/settings", json={"ai_start_times": False})
    assert find(create(client, title="eat dinner"), "eat dinner")["start_at"] is None


def test_nudging_a_task_carries_its_start_time_with_it(client):
    """Left behind, the start time would keep the task reading as urgent from
    a moment that has gone."""
    task = find(create(client, title="overdue thing",
                       deadline=iso_in(hours=-3), start_at=iso_in(hours=-4)),
                "overdue thing")
    target = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    state = client.post("/api/nudge", json={
        "nudges": [{"task_id": task["id"], "deadline": target.isoformat()}]}).json()

    moved = find(state, "overdue thing")
    assert datetime.fromisoformat(moved["deadline"]) == target
    # Still an hour ahead of the deadline, exactly as it was before the nudge.
    assert (target - datetime.fromisoformat(moved["start_at"])) == timedelta(hours=1)


def test_the_calendar_says_when_a_block_was_meant_to_begin(client):
    when = iso_in(hours=4)
    create(client, title="eat dinner", start_at=when, estimated_time=30)
    payload = client.get("/api/calendar").json()
    assert event(payload, "eat dinner")["start_at"] == when


def test_nudging_a_parent_and_its_child_at_once_gives_each_its_own_start(client):
    """The child was named in the same call, so its own new deadline decides
    where it starts — not the slide its parent's move would have given it."""
    parent = find(create(client, title="parent", deadline=iso_in(hours=-3),
                         start_at=iso_in(hours=-4)), "parent")
    child = client.post("/api/tasks", json={
        "title": "child", "parent_id": parent["id"], "annotate": False,
        "deadline": iso_in(hours=-5), "start_at": iso_in(hours=-6)}).json()
    assert find(child, "child")

    base = datetime.now(timezone.utc).replace(microsecond=0)
    p_at, c_at = base + timedelta(days=2), base + timedelta(days=5)
    state = client.post("/api/nudge", json={"nudges": [
        {"task_id": parent["id"], "deadline": p_at.isoformat()},
        {"task_id": find(child, "child")["id"], "deadline": c_at.isoformat()},
    ]}).json()

    kid = find(state, "child")
    assert datetime.fromisoformat(kid["deadline"]) == c_at
    # Its start kept its own one-hour lead, measured from its own new deadline.
    assert c_at - datetime.fromisoformat(kid["start_at"]) == timedelta(hours=1)


# ---------------- the daily AI budget ----------------

def test_the_budget_round_trips_and_the_spend_is_reported(client):
    from app import db
    assert client.get("/api/settings").json()["daily_budget_usd"] == 0.0
    settings = client.put("/api/settings", json={"daily_budget_usd": 2.0}).json()
    assert settings["daily_budget_usd"] == 2.0
    assert settings["spend"] == {"today": 0.0, "budget": 2.0, "stage": 0,
                                 "note": "", "tiers": {"fast": "fast",
                                                       "balanced": "balanced",
                                                       "deep": "deep"}}
    db.record_spend("deep", "claude-opus-5", 1.60)
    spend = client.get("/api/settings").json()["spend"]
    assert spend["today"] == 1.6
    assert spend["stage"] == 2
    assert spend["tiers"] == {"fast": "fast", "balanced": "fast",
                              "deep": "balanced"}


def test_the_page_is_told_about_the_throttle_on_every_read(client):
    """The badge has to appear the moment it is true, not the next time
    someone opens the settings dialog."""
    from app import db
    assert client.get("/api/state").json()["spend"]["stage"] == 0
    db.update_settings({"daily_budget_usd": 1.0})
    db.record_spend("deep", "claude-opus-5", 1.0)
    spend = client.get("/api/state").json()["spend"]
    assert spend["stage"] == 3
    assert spend["note"] == "the daily budget is spent — everything runs on the fast model"


def test_the_derived_spend_is_never_written_back(client):
    """The page echoes the whole settings object back on save; `spend` is a
    reading, not a preference."""
    settings = client.get("/api/settings").json()
    saved = client.put("/api/settings", json=settings).json()
    assert saved["spend"]["today"] == 0.0
    assert "spend" not in db_settings_keys()


def db_settings_keys():
    from app import db
    with db.connect() as conn:
        return {r["k"] for r in conn.execute("SELECT k FROM settings").fetchall()}
