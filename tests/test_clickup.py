"""ClickUp sync: the API client (mocked HTTP, no network) and what a sync
does and does not touch in the database."""

import importlib

import httpx
import pytest

from app import clickup


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    from app import db
    importlib.reload(db)
    db.init()
    return db


# ---------------------------------------------------------------------------
# normalizing one raw ClickUp task
# ---------------------------------------------------------------------------

def test_normalize_converts_due_date_and_appends_url():
    raw = {
        "id": "abc123",
        "name": "Ship the thing",
        "text_content": "Get it out the door.",
        "due_date": "1700000000000",  # ms epoch
        "url": "https://app.clickup.com/t/abc123",
    }
    got = clickup._normalize(raw)
    assert got["id"] == "abc123"
    assert got["title"] == "Ship the thing"
    assert got["deadline"] == "2023-11-14T22:13:20+00:00"
    assert got["description"] == "Get it out the door.\n\nhttps://app.clickup.com/t/abc123"


def test_normalize_handles_missing_fields():
    got = clickup._normalize({"id": "x", "name": "", "due_date": None})
    assert got["title"] == "Untitled ClickUp task"
    assert got["deadline"] is None
    assert got["description"] == ""


def test_normalize_falls_back_to_description_when_no_text_content():
    got = clickup._normalize({"id": "x", "name": "t", "description": "plain text"})
    assert got["description"] == "plain text"


def test_normalize_ignores_garbage_due_date():
    got = clickup._normalize({"id": "x", "name": "t", "due_date": "not-a-number"})
    assert got["deadline"] is None


# ---------------------------------------------------------------------------
# resolving the token
# ---------------------------------------------------------------------------

def test_token_from_settings():
    assert clickup._token({"clickup_api_token": "pk_abc"}) == "pk_abc"


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_env")
    assert clickup._token({"clickup_api_token": ""}) == "pk_env"


def test_settings_token_wins_over_env(monkeypatch):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_env")
    assert clickup._token({"clickup_api_token": "pk_settings"}) == "pk_settings"


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    with pytest.raises(clickup.ClickUpUnavailable):
        clickup._token({"clickup_api_token": ""})


# ---------------------------------------------------------------------------
# fetch_assigned_tasks — HTTP mocked with httpx.MockTransport, no network
# ---------------------------------------------------------------------------

def _paged_task_batch(n, prefix="t"):
    return [{"id": f"{prefix}{i}", "name": f"task {i}", "due_date": None}
            for i in range(n)]


def test_fetch_paginates_within_a_team(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/api/v2/user":
            return httpx.Response(200, json={"user": {"id": 42}})
        if request.url.path == "/api/v2/team":
            return httpx.Response(200, json={"teams": [{"id": "team1"}]})
        if request.url.path == "/api/v2/team/team1/task":
            page = int(request.url.params.get("page", "0"))
            assert request.url.params.get("assignees[]") == "42"
            batch = _paged_task_batch(100) if page == 0 else _paged_task_batch(3, "p2-")
            return httpx.Response(200, json={"tasks": batch if page < 2 else []})
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(clickup, "_client",
                        lambda token: httpx.Client(base_url=clickup.API_BASE,
                                                   transport=httpx.MockTransport(handler)))
    tasks = clickup.fetch_assigned_tasks("pk_test")
    assert len(tasks) == 103  # 100 on page 0, 3 on page 1, page 2 empty -> stop
    assert calls[0].endswith("/api/v2/user")


def test_fetch_dedupes_tasks_seen_in_more_than_one_team(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/user":
            return httpx.Response(200, json={"user": {"id": 1}})
        if request.url.path == "/api/v2/team":
            return httpx.Response(200, json={"teams": [{"id": "a"}, {"id": "b"}]})
        # Both "teams" happen to return the same task id — a re-share, or a
        # quirk of the API; either way it must only appear once.
        return httpx.Response(200, json={"tasks": [{"id": "dup", "name": "x"}]})

    monkeypatch.setattr(clickup, "_client",
                        lambda token: httpx.Client(base_url=clickup.API_BASE,
                                                   transport=httpx.MockTransport(handler)))
    tasks = clickup.fetch_assigned_tasks("pk_test")
    assert len(tasks) == 1


def test_fetch_maps_401_to_clickup_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"err": "Invalid token"})

    monkeypatch.setattr(clickup, "_client",
                        lambda token: httpx.Client(base_url=clickup.API_BASE,
                                                   transport=httpx.MockTransport(handler)))
    with pytest.raises(clickup.ClickUpUnavailable):
        clickup.fetch_assigned_tasks("bad-token")


def test_fetch_maps_network_error_to_clickup_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    monkeypatch.setattr(clickup, "_client",
                        lambda token: httpx.Client(base_url=clickup.API_BASE,
                                                   transport=httpx.MockTransport(handler)))
    with pytest.raises(clickup.ClickUpUnavailable):
        clickup.fetch_assigned_tasks("pk_test")


# ---------------------------------------------------------------------------
# sync() — what lands in the database
# ---------------------------------------------------------------------------

def _remote(id="c1", title="Do the thing", description="", deadline=None):
    return {"id": id, "title": title, "description": description, "deadline": deadline}


def test_sync_creates_a_clickup_project_and_its_tasks(monkeypatch, temp_db):
    db = temp_db
    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Task one"),
                                       _remote("c2", "Task two")])
    result = clickup.sync({"clickup_api_token": "pk_test"})
    assert result["fetched"] == 2
    assert len(result["created"]) == 2
    assert result["updated"] == []

    projects = db.list_projects()
    assert [p["name"] for p in projects] == ["Tasks", "ClickUp"]
    project = next(p for p in projects if p["name"] == "ClickUp")
    tasks = db.list_tasks(project["id"])
    assert {t["title"] for t in tasks} == {"Task one", "Task two"}
    assert {t["clickup_id"] for t in tasks} == {"c1", "c2"}
    assert db.get_settings()["clickup_last_sync_at"]


def test_sync_reuses_existing_clickup_project(monkeypatch, temp_db):
    db = temp_db
    existing = db.create_project("ClickUp")
    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Task one")])
    result = clickup.sync({"clickup_api_token": "pk_test"})
    assert result["project_id"] == existing["id"]
    assert len(db.list_projects()) == 2  # "Tasks" + the pre-existing "ClickUp"


def test_sync_updates_by_clickup_id_instead_of_duplicating(monkeypatch, temp_db):
    db = temp_db
    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Original title")])
    clickup.sync({"clickup_api_token": "pk_test"})

    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Renamed title",
                                               deadline="2030-01-01T00:00:00+00:00")])
    result = clickup.sync({"clickup_api_token": "pk_test"})
    assert result["created"] == []
    assert len(result["updated"]) == 1

    project = next(p for p in db.list_projects() if p["name"] == "ClickUp")
    tasks = db.list_tasks(project["id"])
    assert len(tasks) == 1  # no duplicate
    assert tasks[0]["title"] == "Renamed title"
    assert tasks[0]["deadline"] == "2030-01-01T00:00:00+00:00"


def test_sync_does_not_relocate_a_task_you_moved_yourself(monkeypatch, temp_db):
    db = temp_db
    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Original title")])
    clickup.sync({"clickup_api_token": "pk_test"})
    clickup_project = next(p for p in db.list_projects() if p["name"] == "ClickUp")
    task = db.list_tasks(clickup_project["id"])[0]

    elsewhere = db.create_project("Work")
    db.move_task_to_project(task["id"], elsewhere["id"])

    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Renamed title")])
    clickup.sync({"clickup_api_token": "pk_test"})

    moved = db.get_task(task["id"])
    assert moved["title"] == "Renamed title"     # still refreshed
    assert moved["project_id"] == elsewhere["id"]  # but not dragged back


def test_sync_leaves_fields_it_does_not_own_alone(monkeypatch, temp_db):
    db = temp_db
    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Original title")])
    clickup.sync({"clickup_api_token": "pk_test"})
    project = next(p for p in db.list_projects() if p["name"] == "ClickUp")
    task = db.list_tasks(project["id"])[0]
    db.update_task(task["id"], {"estimated_time": 45, "impact": 8, "effort": 2,
                                "status": "in_progress"})

    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: [_remote("c1", "Original title")])  # unchanged
    clickup.sync({"clickup_api_token": "pk_test"})

    kept = db.get_task(task["id"])
    assert kept["estimated_time"] == 45
    assert kept["impact"] == 8 and kept["effort"] == 2
    assert kept["status"] == "in_progress"


def test_sync_without_a_token_raises_and_does_nothing(monkeypatch, temp_db):
    db = temp_db
    called = []
    monkeypatch.setattr(clickup, "fetch_assigned_tasks", lambda token: called.append(1))
    with pytest.raises(clickup.ClickUpUnavailable):
        clickup.sync({"clickup_api_token": ""})
    assert called == []
    assert db.list_projects() == [db.ensure_project()]  # no "ClickUp" tab appeared


# ---------------------------------------------------------------------------
# run_once() — what the background loop calls each tick
# ---------------------------------------------------------------------------

def test_run_once_skips_quietly_without_a_token(monkeypatch, temp_db):
    monkeypatch.setattr(clickup, "fetch_assigned_tasks",
                        lambda token: (_ for _ in ()).throw(
                            AssertionError("should never be called")))
    assert clickup.run_once() is None


def test_run_once_syncs_when_a_token_is_configured(monkeypatch, temp_db):
    db = temp_db
    db.update_settings({"clickup_api_token": "pk_test"})
    monkeypatch.setattr(clickup, "fetch_assigned_tasks", lambda token: [_remote()])
    result = clickup.run_once()
    assert result["fetched"] == 1
