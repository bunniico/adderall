"""Tests for the deterministic scheduling core — no AI, no network."""

from datetime import datetime, timedelta, timezone

from app import logic

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
SETTINGS = {
    "buffer": 0.30,
    "adaptive_buffer": False,
    "matrix_threshold": 5,
    "auto_deadlines": True,
}


def make_task(id, **kw):
    base = {
        "id": id, "title": id, "description": "", "parent_id": None,
        "deadline": None, "estimated_time": None, "actual_time": None,
        "impact": None, "effort": None, "status": "todo",
        "ack_thankless": False, "order_index": 0,
        "started_at": None,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    base.update(kw)
    return base


# ---- time-tax buffer ----

def test_buffered_estimate_applies_time_tax():
    assert logic.buffered_estimate(60, 0.30) == 78
    assert logic.buffered_estimate(45, 0.30) == 59  # example from the design doc
    assert logic.buffered_estimate(None, 0.30) is None
    assert logic.buffered_estimate(1, 0.25) >= 1


def test_effective_buffer_clamped_to_range():
    assert logic.effective_buffer({"buffer": 0.10}, []) == 0.25
    assert logic.effective_buffer({"buffer": 0.90}, []) == 0.50
    assert logic.effective_buffer({"buffer": 0.30}, []) == 0.30


def test_adaptive_buffer_raises_never_lowers():
    s = {"buffer": 0.30, "adaptive_buffer": True}
    # user overshoots estimates by 45% on average -> buffer learns upward
    assert logic.effective_buffer(s, [1.5, 1.4, 1.45]) == 0.45
    # user is accurate -> buffer stays at configured base
    assert logic.effective_buffer(s, [1.0, 0.9, 1.05]) == 0.30
    # capped at 50%
    assert logic.effective_buffer(s, [2.0, 2.2, 1.9]) == 0.50
    # fewer than 3 data points -> no learning yet
    assert logic.effective_buffer(s, [2.0, 2.0]) == 0.30


# ---- action priority matrix ----

def test_quadrants():
    assert logic.quadrant(8, 2) == "quick_win"
    assert logic.quadrant(8, 8) == "major_project"
    assert logic.quadrant(2, 2) == "fill_in"
    assert logic.quadrant(2, 8) == "thankless"
    assert logic.quadrant(None, 5) is None
    assert logic.quadrant(5, 5) == "major_project"  # threshold is inclusive


def test_quadrant_custom_threshold():
    assert logic.quadrant(4, 2, threshold=3) == "quick_win"
    assert logic.quadrant(4, 2, threshold=5) == "fill_in"


# ---- urgency ----

def test_urgency_no_deadline_is_low_baseline():
    assert logic.urgency(None, 60, NOW) == 0.5


def test_urgency_overdue_is_max():
    assert logic.urgency(NOW - timedelta(hours=1), 60, NOW) == 10.0


def test_urgency_rises_as_slack_shrinks():
    tight = logic.urgency(NOW + timedelta(minutes=90), 78, NOW)   # little slack
    loose = logic.urgency(NOW + timedelta(days=7), 78, NOW)       # lots of slack
    assert tight > loose
    # no slack at all -> pegged at 10
    assert logic.urgency(NOW + timedelta(minutes=60), 78, NOW) == 10.0


# ---- compute: derived fields, auto-deadlines, backward scheduling ----

def test_compute_basic_derivations():
    t = make_task("a", estimated_time=60, impact=8, effort=2)
    d = logic.compute([t], SETTINGS, now=NOW)["a"]
    assert d["buffered_estimate"] == 78
    assert d["quadrant"] == "quick_win"
    assert d["deadline_source"] == "auto"
    # quick win horizon = 1 day from creation
    assert d["deadline"] == (NOW + timedelta(days=1)).isoformat(timespec="seconds")


def test_user_deadline_wins_over_auto():
    dl = (NOW + timedelta(days=2)).isoformat()
    t = make_task("a", deadline=dl, impact=8, effort=2)
    d = logic.compute([t], SETTINGS, now=NOW)["a"]
    assert d["deadline_source"] == "user"


def test_auto_deadlines_toggle_off():
    t = make_task("a", impact=8, effort=2)
    d = logic.compute([t], {**SETTINGS, "auto_deadlines": False}, now=NOW)["a"]
    assert d["deadline"] is None
    assert d["deadline_source"] == "none"
    assert d["urgency"] == 0.5  # low baseline


def test_backward_scheduling_children_from_parent_deadline():
    parent_dl = NOW + timedelta(hours=10)
    parent = make_task("p", deadline=parent_dl.isoformat())
    c1 = make_task("c1", parent_id="p", estimated_time=60, order_index=0)  # buffered 78
    c2 = make_task("c2", parent_id="p", estimated_time=30, order_index=1)  # buffered 39
    c3 = make_task("c3", parent_id="p", estimated_time=20, order_index=2)  # buffered 26
    derived = logic.compute([parent, c1, c2, c3], SETTINGS, now=NOW)
    # last child finishes at the parent deadline
    assert derived["c3"]["deadline"] == parent_dl.isoformat(timespec="seconds")
    # earlier children leave room for later siblings' buffered estimates
    assert derived["c2"]["deadline"] == (parent_dl - timedelta(minutes=26)).isoformat(timespec="seconds")
    assert derived["c1"]["deadline"] == (parent_dl - timedelta(minutes=26 + 39)).isoformat(timespec="seconds")


def test_backward_scheduling_skips_done_siblings():
    parent_dl = NOW + timedelta(hours=10)
    parent = make_task("p", deadline=parent_dl.isoformat())
    c1 = make_task("c1", parent_id="p", estimated_time=60, order_index=0)
    c2 = make_task("c2", parent_id="p", estimated_time=30, order_index=1, status="done")
    derived = logic.compute([parent, c1, c2], SETTINGS, now=NOW)
    # done sibling reserves no time
    assert derived["c1"]["deadline"] == parent_dl.isoformat(timespec="seconds")


def test_parent_with_open_children_is_not_actionable():
    parent = make_task("p")
    child = make_task("c", parent_id="p")
    derived = logic.compute([parent, child], SETTINGS, now=NOW)
    assert not derived["p"]["actionable"]
    assert derived["c"]["actionable"]


def test_done_and_discarded_not_actionable():
    derived = logic.compute(
        [make_task("a", status="done"), make_task("b", status="discarded")],
        SETTINGS, now=NOW)
    assert not derived["a"]["actionable"]
    assert not derived["b"]["actionable"]


# ---- next task ordering ----

def test_next_task_prefers_urgent_then_quick_wins():
    urgent = make_task("urgent", deadline=(NOW + timedelta(minutes=90)).isoformat(),
                       estimated_time=60, impact=2, effort=8)
    quick = make_task("quick", impact=8, effort=2, estimated_time=10)
    thankless = make_task("thankless", impact=2, effort=8, estimated_time=10)
    tasks = [thankless, quick, urgent]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    assert logic.next_task(tasks, derived)["id"] == "urgent"
    # without the urgent one, the quick win beats equal-urgency thankless work
    tasks2 = [thankless, quick]
    derived2 = logic.compute(tasks2, {**SETTINGS, "auto_deadlines": False}, now=NOW)
    assert logic.next_task(tasks2, derived2)["id"] == "quick"


def test_next_task_none_when_empty():
    assert logic.next_task([], {}) is None
