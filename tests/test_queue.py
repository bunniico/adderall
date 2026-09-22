"""The retry queue: persistence in db.py, and the sweep in queue.py that
processes what it holds."""

import importlib

import pytest


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDERALL_DB", str(tmp_path / "test.db"))
    from app import db
    importlib.reload(db)
    db.init()
    return db


@pytest.fixture()
def q(temp_db):
    from app import queue
    importlib.reload(queue)
    return queue


# ---------------- db.queue_* persistence ----------------

def test_queue_add_and_pending_round_trip(temp_db):
    row = temp_db.queue_add("breakdown", {"task_id": "abc"})
    assert row["kind"] == "breakdown"
    assert row["payload"] == {"task_id": "abc"}
    assert row["attempts"] == 0
    [pending] = temp_db.queue_pending()
    assert pending["id"] == row["id"]
    assert pending["payload"] == {"task_id": "abc"}


def test_queue_retry_bumps_attempts_and_keeps_the_item(temp_db):
    row = temp_db.queue_add("breakdown", {"task_id": "abc"})
    temp_db.queue_retry(row["id"], "still no internet")
    [pending] = temp_db.queue_pending()
    assert pending["attempts"] == 1
    assert pending["last_error"] == "still no internet"


def test_queue_remove_drops_the_item(temp_db):
    row = temp_db.queue_add("breakdown", {"task_id": "abc"})
    temp_db.queue_remove(row["id"])
    assert temp_db.queue_pending() == []


def test_queue_pending_is_ordered_oldest_first(temp_db):
    first = temp_db.queue_add("breakdown", {"n": 1})
    second = temp_db.queue_add("breakdown", {"n": 2})
    ids = [row["id"] for row in temp_db.queue_pending()]
    assert ids == [first["id"], second["id"]]


# ---------------- queue.py: registering and running handlers ----------------

def test_enqueue_requires_a_registered_handler(q):
    with pytest.raises(ValueError):
        q.enqueue("nonsense", {})


def test_run_once_removes_an_item_that_succeeds(temp_db, q):
    calls = []
    q.register("ok", calls.append)
    q.enqueue("ok", {"n": 1})
    assert q.run_once() == 1
    assert calls == [{"n": 1}]
    assert temp_db.queue_pending() == []


def test_run_once_keeps_an_item_that_hits_network_retry(temp_db, q):
    def boom(payload):
        raise q.NetworkRetry("no route to the internet")
    q.register("flaky", boom)
    q.enqueue("flaky", {})
    q.run_once()
    [pending] = temp_db.queue_pending()
    assert pending["attempts"] == 1
    assert pending["last_error"] == "no route to the internet"
    # Still there on the next sweep too — nothing about a NetworkRetry drops it.
    q.run_once()
    [pending] = temp_db.queue_pending()
    assert pending["attempts"] == 2


def test_run_once_drops_an_item_that_fails_for_good(temp_db, q):
    def boom(payload):
        raise ValueError("bad payload")
    q.register("broken", boom)
    q.enqueue("broken", {})
    q.run_once()
    assert temp_db.queue_pending() == []


def test_run_once_drops_an_item_with_no_handler_registered(temp_db, q):
    calls = []
    q.register("temp", calls.append)
    q.enqueue("temp", {})
    q._HANDLERS.clear()  # simulate a kind nothing claims any more
    q.run_once()
    assert temp_db.queue_pending() == []


def test_run_once_processes_every_pending_item(temp_db, q):
    seen = []
    q.register("ok", seen.append)
    q.enqueue("ok", {"n": 1})
    q.enqueue("ok", {"n": 2})
    assert q.run_once() == 2
    assert seen == [{"n": 1}, {"n": 2}]
