"""The module switches: each module's routes, jobs and queued work stop while
it is off, and its data is still there when it comes back on."""

import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


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
    monkeypatch.setattr(main.ai, "annotate", lambda settings, tasks, **kw: {
        t["id"]: {"id": t["id"], "minutes": 30, "impact": 7, "effort": 3}
        for t in tasks})
    monkeypatch.setattr(main.ai, "breakdown", lambda *a, **kw: ["step 1", "step 2"])
    monkeypatch.setattr(main.ai, "compile_braindump",
                        lambda settings, text: [{"title": "from the dump"}])
    monkeypatch.setattr(main.ai, "extract_schedule",
                        lambda settings, title, now_local: {"has_deadline": False,
                                                            "has_repeat": False})
    return main


@pytest.fixture()
def client(main):
    return TestClient(main.app)


def turn(client, **switches):
    res = client.put("/api/settings", json={"modules": switches})
    assert res.status_code == 200, res.text
    return res.json()


def add(client, title="task", **kw):
    res = client.post("/api/tasks", json={"title": title, **kw})
    assert res.status_code == 201, res.text
    return next(t for t in res.json()["tasks"] if t["title"] == title)


def test_every_module_is_on_by_default_and_listed(client):
    from app import modules
    settings = client.get("/api/settings").json()
    assert settings["modules"] == {m.id: True for m in modules.MODULES}
    assert [m["id"] for m in settings["module_catalog"]] == [m.id for m in modules.MODULES]
    assert {"id", "name", "group", "description"} <= set(settings["module_catalog"][0])


def test_switches_save_one_at_a_time(client):
    turn(client, calendar=False)
    turn(client, habits=False)
    stored = client.get("/api/settings").json()["modules"]
    assert stored["calendar"] is False and stored["habits"] is False
    assert stored["overview"] is True
    turn(client, calendar=True)
    assert client.get("/api/settings").json()["modules"]["calendar"] is True


# (module, method, path) for one route of each module that has any. The task
# id is filled in by the test.
ROUTES = [
    ("clickup", "post", "/api/clickup/sync", None),
    ("magic_todo", "post", "/api/tasks/{id}/breakdown", {}),
    ("braindump", "post", "/api/compile", {"text": "milk"}),
    ("ai_estimates", "post", "/api/tasks/{id}/annotate", None),
    ("calendar", "get", "/api/calendar", None),
    ("overview", "get", "/api/overview", None),
    ("habits", "get", "/api/habits", None),
    ("help", "get", "/api/help", None),
    ("recurring", "put", "/api/tasks/{id}/repeat", {"freq": "daily"}),
    ("focus", "get", "/api/focus", None),
    ("alarms", "get", "/api/events", None),
]


@pytest.mark.parametrize("module,method,path,body", ROUTES)
def test_a_modules_routes_are_gone_while_it_is_off(client, module, method, path, body):
    task = add(client)
    turn(client, **{module: False})
    kw = {"json": body} if body is not None else {}
    res = getattr(client, method)(path.format(id=task["id"]), **kw)
    assert res.status_code == 404
    assert "turned off" in res.json()["detail"]


@pytest.mark.parametrize("module,method,path,body",
                         [r for r in ROUTES if r[0] not in ("alarms", "clickup")])
def test_and_back_when_it_is_on_again(client, module, method, path, body):
    # The event stream never ends and ClickUp needs a token, so neither is
    # asked twice here.
    task = add(client)
    turn(client, **{module: False})
    turn(client, **{module: True})
    kw = {"json": body} if body is not None else {}
    res = getattr(client, method)(path.format(id=task["id"]), **kw)
    assert res.status_code == 200, res.text


def test_the_mcp_server_is_gone_while_it_is_off(main):
    client = TestClient(main.app, base_url="http://localhost:8000")
    turn(client, mcp=False)
    res = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert res.status_code == 404


def test_no_ai_estimate_while_ai_estimates_is_off(client):
    turn(client, ai_estimates=False)
    assert add(client, "unestimated")["estimated_time"] is None
    turn(client, ai_estimates=True)
    assert add(client, "estimated")["estimated_time"] == 30


def test_a_lens_tab_cannot_be_opened_while_its_module_is_off(client):
    turn(client, overview=False)
    assert client.post("/api/projects/overview/activate").status_code == 404


def test_a_lens_tab_open_when_its_module_goes_off_closes(client):
    assert client.post("/api/projects/habits/activate").json()["habits"] is True
    turn(client, habits=False)
    assert client.get("/api/state").json()["habits"] is False


def test_with_repeating_off_a_repeat_in_the_title_is_just_words(client):
    turn(client, recurring=False)
    task = add(client, "water plants every day")
    assert task["recurrence"] is None
    assert task["series_id"] is None


def test_with_repeating_off_a_rhythm_waits_and_its_badge_goes(client):
    from app import db, recurring
    task = add(client, "bins")
    res = client.put(f"/api/tasks/{task['id']}/repeat", json={"freq": "daily"})
    assert res.status_code == 200, res.text
    series = db.get_series(db.get_task(task["id"])["series_id"])

    turn(client, recurring=False)
    state = client.post(f"/api/tasks/{task['id']}/complete", json={}).json()
    assert all(t["recurrence"] is None for t in state["tasks"])
    assert [t["title"] for t in state["tasks"]] == ["bins"]  # no next copy
    assert recurring.sweep()["created"] == []
    assert recurring.forecast() == []
    assert db.get_series(series["id"])["next_at"] == series["next_at"]

    turn(client, recurring=True)
    assert db.get_series(series["id"])["active"]


def test_queued_work_waits_for_its_module(main, client, monkeypatch):
    task = add(client)

    def offline(*a, **kw):
        raise main.ai.AINetworkError("no route to the internet")

    monkeypatch.setattr(main.ai, "breakdown", offline)
    res = client.post(f"/api/tasks/{task['id']}/breakdown", json={})
    assert res.json()["queued"]["kind"] == "breakdown"

    monkeypatch.setattr(main.ai, "breakdown", lambda *a, **kw: ["step a"])
    turn(client, magic_todo=False)
    main.queue.run_once()
    assert len(main.db.queue_pending()) == 1  # kept, not run and not dropped

    turn(client, magic_todo=True)
    main.queue.run_once()
    assert main.db.queue_pending() == []
    titles = [t["title"] for t in client.get("/api/state").json()["tasks"][0]["subtasks"]]
    assert titles == ["step a"]


def test_the_clickup_job_skips_its_turn_while_off(main, client, monkeypatch):
    calls = []
    monkeypatch.setattr(main.clickup, "sync", lambda settings=None: calls.append(1) or {})
    client.put("/api/settings", json={"clickup_api_token": "pk_test"})
    turn(client, clickup=False)
    assert main.clickup.run_once() is None
    turn(client, clickup=True)
    main.clickup.run_once()
    assert calls == [1]


def test_old_xp_and_alarm_switches_carry_over(monkeypatch, tmp_path):
    """Before modules, XP and alarms had switches of their own."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.execute("INSERT INTO settings VALUES ('gamification', ?)", (json.dumps(False),))
    conn.execute("INSERT INTO settings VALUES ('alarms', ?)",
                 (json.dumps({"enabled": False, "stop_lead": 30}),))
    conn.commit()
    conn.close()

    monkeypatch.setenv("ADDERALL_DB", str(path))
    from app import db
    importlib.reload(db)
    db.init()
    switches = db.get_settings()["modules"]
    assert switches["xp"] is False
    assert switches["alarms"] is False
    assert switches["calendar"] is True
