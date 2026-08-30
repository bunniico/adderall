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


# ---- manual ordering ----

MANUAL = {**SETTINGS, "manual_order": True}


def test_order_path_locates_each_task_in_the_tree():
    p = make_task("p", order_index=0)
    c1 = make_task("c1", parent_id="p", order_index=0)
    c2 = make_task("c2", parent_id="p", order_index=1)
    other = make_task("other", order_index=1)
    derived = logic.compute([p, c1, c2, other], SETTINGS, now=NOW)
    assert derived["p"]["order_path"] == [0]
    assert derived["c1"]["order_path"] == [0, 0]
    assert derived["c2"]["order_path"] == [0, 1]
    assert derived["other"]["order_path"] == [1]


def test_manual_order_sorts_by_position_not_urgency():
    urgent = make_task("urgent", order_index=1, estimated_time=60,
                       deadline=(NOW + timedelta(minutes=70)).isoformat())
    calm = make_task("calm", order_index=0, impact=8, effort=2, estimated_time=10)
    tasks = [urgent, calm]
    auto = logic.compute(tasks, SETTINGS, now=NOW)
    assert sorted(tasks, key=lambda t: auto[t["id"]]["sort_key"])[0]["id"] == "urgent"
    manual = logic.compute(tasks, MANUAL, now=NOW)
    assert sorted(tasks, key=lambda t: manual[t["id"]]["sort_key"])[0]["id"] == "calm"
    # ...and "what next" reads the list you arranged, top to bottom
    assert logic.next_task(tasks, manual)["id"] == "calm"


def test_manual_order_reaches_into_subtasks():
    first = make_task("first", order_index=0)
    step = make_task("step", parent_id="first", order_index=0)
    second = make_task("second", order_index=1, estimated_time=10,
                       deadline=(NOW + timedelta(minutes=11)).isoformat())
    tasks = [first, step, second]
    derived = logic.compute(tasks, MANUAL, now=NOW)
    # "first" holds an open subtask, so the step under it is what's actionable
    assert logic.next_task(tasks, derived)["id"] == "step"
    assert logic.focus_root_id(tasks, derived) == "first"


def test_manual_order_leaves_the_rest_of_the_derivations_alone():
    t = make_task("a", estimated_time=60, impact=8, effort=2)
    auto = logic.compute([t], SETTINGS, now=NOW)["a"]
    manual = logic.compute([t], MANUAL, now=NOW)["a"]
    for field in ("buffered_estimate", "quadrant", "urgency", "deadline",
                  "deadline_source", "rollup_estimate", "actionable"):
        assert auto[field] == manual[field]


# ---- subtree rollups (a container is worth what it holds) ----

def test_rollup_estimate_sums_subtasks():
    parent = make_task("p", estimated_time=15)          # own buffered: 20
    kids = [make_task(f"c{i}", parent_id="p", order_index=i, estimated_time=m)
            for i, m in enumerate([10, 20, 30])]        # buffered: 13 + 26 + 39
    derived = logic.compute([parent, *kids], SETTINGS, now=NOW)
    assert derived["p"]["buffered_estimate"] == 20      # own estimate is unchanged
    assert derived["p"]["rollup_estimate"] == 13 + 26 + 39
    assert derived["p"]["has_subtasks"] is True
    assert derived["c0"]["rollup_estimate"] == 13
    assert derived["c0"]["has_subtasks"] is False


def test_rollup_is_recursive_through_grandchildren():
    parent = make_task("p", estimated_time=15)
    child = make_task("c", parent_id="p", estimated_time=99)
    g1 = make_task("g1", parent_id="c", order_index=0, estimated_time=10)  # 13
    g2 = make_task("g2", parent_id="c", order_index=1, estimated_time=20)  # 26
    derived = logic.compute([parent, child, g1, g2], SETTINGS, now=NOW)
    assert derived["c"]["rollup_estimate"] == 39
    assert derived["p"]["rollup_estimate"] == 39  # the grandchildren, not the 99


def test_rollup_falls_back_to_own_estimate_when_subtasks_have_none():
    parent = make_task("p", estimated_time=60)
    child = make_task("c", parent_id="p")
    derived = logic.compute([parent, child], SETTINGS, now=NOW)
    assert derived["p"]["rollup_estimate"] == 78


def test_rollup_tracks_done_and_remaining_time():
    parent = make_task("p")
    c1 = make_task("c1", parent_id="p", order_index=0, estimated_time=10, status="done")
    c2 = make_task("c2", parent_id="p", order_index=1, estimated_time=20)
    derived = logic.compute([parent, c1, c2], SETTINGS, now=NOW)
    assert derived["p"]["rollup_estimate"] == 13 + 26
    assert derived["p"]["rollup_done"] == 13
    assert derived["p"]["rollup_remaining"] == 26


def test_rollup_excludes_discarded_branches():
    parent = make_task("p")
    c1 = make_task("c1", parent_id="p", order_index=0, estimated_time=10)
    c2 = make_task("c2", parent_id="p", order_index=1, estimated_time=20,
                   status="discarded")
    derived = logic.compute([parent, c1, c2], SETTINGS, now=NOW)
    assert derived["p"]["rollup_estimate"] == 13  # the discarded branch is gone


def test_done_parent_counts_as_fully_done():
    parent = make_task("p", status="done")
    c1 = make_task("c1", parent_id="p", estimated_time=10, status="done")
    derived = logic.compute([parent, c1], SETTINGS, now=NOW)
    assert derived["p"]["rollup_done"] == derived["p"]["rollup_estimate"] == 13
    assert derived["p"]["rollup_remaining"] == 0


def test_rollup_deadline_is_the_furthest_inside():
    parent = make_task("p", deadline=(NOW + timedelta(hours=2)).isoformat())
    early = make_task("c1", parent_id="p", order_index=0, estimated_time=10)
    late = make_task("c2", parent_id="p", order_index=1,
                     deadline=(NOW + timedelta(days=3)).isoformat())
    derived = logic.compute([parent, early, late], SETTINGS, now=NOW)
    assert derived["p"]["rollup_deadline"] == \
        (NOW + timedelta(days=3)).isoformat(timespec="seconds")
    assert derived["p"]["rollup_deadline_source"] == "user"
    # the container's own deadline is left alone for scheduling
    assert derived["p"]["deadline"] == (NOW + timedelta(hours=2)).isoformat(timespec="seconds")


def test_backward_scheduled_children_end_at_the_parent_deadline():
    parent_dl = NOW + timedelta(hours=10)
    parent = make_task("p", deadline=parent_dl.isoformat())
    c1 = make_task("c1", parent_id="p", order_index=0, estimated_time=60)
    c2 = make_task("c2", parent_id="p", order_index=1, estimated_time=30)
    derived = logic.compute([parent, c1, c2], SETTINGS, now=NOW)
    # nothing inside runs past the container, so the rollup matches it
    assert derived["p"]["rollup_deadline"] == parent_dl.isoformat(timespec="seconds")


def test_container_urgency_uses_remaining_subtree_work():
    parent = make_task("p", deadline=(NOW + timedelta(hours=2)).isoformat())
    child = make_task("c", parent_id="p", estimated_time=90)  # buffered 117 > 120 slack
    derived = logic.compute([parent, child], SETTINGS, now=NOW)
    assert derived["p"]["urgency"] > logic.urgency(
        NOW + timedelta(hours=2), logic.DEFAULT_ESTIMATE_MIN, NOW)


# ---- focus traversal ----

def test_focus_queue_is_depth_first_children_before_parent():
    p = make_task("p")
    c1 = make_task("c1", parent_id="p", order_index=0)
    c2 = make_task("c2", parent_id="p", order_index=1)
    g1 = make_task("g1", parent_id="c1", order_index=0)
    g2 = make_task("g2", parent_id="c1", order_index=1)
    queue = logic.focus_queue([p, c1, c2, g1, g2], "p")
    assert [t["id"] for t in queue] == ["g1", "g2", "c1", "c2", "p"]


def test_focus_queue_skips_finished_branches():
    p = make_task("p")
    c1 = make_task("c1", parent_id="p", order_index=0, status="done")
    g1 = make_task("g1", parent_id="c1", order_index=0)  # under a done parent
    c2 = make_task("c2", parent_id="p", order_index=1, status="discarded")
    c3 = make_task("c3", parent_id="p", order_index=2)
    queue = logic.focus_queue([p, c1, g1, c2, c3], "p")
    assert [t["id"] for t in queue] == ["c3", "p"]


def test_focus_queue_scoped_to_a_subtree():
    p = make_task("p")
    c1 = make_task("c1", parent_id="p", order_index=0)
    g1 = make_task("g1", parent_id="c1", order_index=0)
    other = make_task("other")
    queue = logic.focus_queue([p, c1, g1, other], "c1")
    assert [t["id"] for t in queue] == ["g1", "c1"]


def test_focus_queue_unknown_root_is_empty():
    assert logic.focus_queue([make_task("a")], "nope") == []


def test_focus_root_is_the_tree_owning_the_most_urgent_step():
    calm = make_task("calm", impact=8, effort=2, estimated_time=10)
    project = make_task("project")
    step = make_task("step", parent_id="project", estimated_time=60,
                     deadline=(NOW + timedelta(minutes=70)).isoformat())
    tasks = [calm, project, step]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    # the urgent step is nested; the session roots at its top-level ancestor
    assert logic.next_task(tasks, derived)["id"] == "step"
    assert logic.focus_root_id(tasks, derived) == "project"


def test_focus_root_none_when_nothing_actionable():
    assert logic.focus_root_id([], {}) is None


def test_ancestor_titles():
    p = make_task("p", title="Project")
    c = make_task("c", parent_id="p", title="Chunk")
    g = make_task("g", parent_id="c", title="Step")
    assert logic.ancestor_titles([p, c, g], g) == ["Project", "Chunk"]
    assert logic.ancestor_titles([p, c, g], p) == []


# ---- priority score ----

def test_priority_score_ranks_urgent_important_cheap_work_first():
    urgent_quick_win = logic.priority_score(9, 2, 10.0)
    distant_thankless = logic.priority_score(2, 9, 0.5)
    assert urgent_quick_win > distant_thankless
    assert 0 <= distant_thankless <= 100
    assert urgent_quick_win <= 100


def test_priority_score_is_neutral_for_unscored_tasks():
    """An unrated task is unknown, not worthless — it must not sink to zero."""
    unscored = logic.priority_score(None, None, 5.0)
    assert unscored == logic.priority_score(5, 5, 5.0)
    assert unscored > logic.priority_score(0, 10, 5.0)


def test_priority_score_effort_only_breaks_ties():
    """Same deadline pressure and impact: the cheaper task comes first."""
    cheap = logic.priority_score(7, 1, 6.0)
    dear = logic.priority_score(7, 9, 6.0)
    assert cheap > dear
    # ...but impact outweighs it, so a cheap trivial task never beats a
    # valuable one on effort alone.
    assert logic.priority_score(9, 9, 6.0) > logic.priority_score(2, 0, 6.0)


def test_compute_scores_every_task():
    tasks = [
        make_task("a", impact=9, effort=2, estimated_time=30,
                  deadline=(NOW + timedelta(minutes=30)).isoformat()),
        make_task("b", impact=2, effort=9, estimated_time=30, order_index=1,
                  deadline=(NOW + timedelta(days=30)).isoformat()),
    ]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    assert derived["a"]["score"] > derived["b"]["score"]
    assert all(0 <= derived[t["id"]]["score"] <= 100 for t in tasks)


def test_container_scores_off_the_work_it_still_holds():
    """A parent's score follows the work in its subtree, not its own estimate.

    An hour away with five minutes of its own work looks relaxed; an hour away
    with an hour of subtasks under it does not, and the score has to say so.
    """
    parent = make_task("p", impact=8, effort=3, estimated_time=5,
                       deadline=(NOW + timedelta(minutes=60)).isoformat())
    kid = make_task("k", parent_id="p", estimated_time=60)
    derived = logic.compute([parent, kid], SETTINGS, now=NOW)
    assert derived["p"]["urgency"] == 10.0        # 78m of work, 60m left
    assert derived["p"]["score"] == logic.priority_score(8, 3, 10.0)
    # The same task judged on its own five minutes would barely register.
    assert derived["p"]["score"] > logic.priority_score(8, 3, 1.2)


# ---- nudging a past-due plan forward ----

def test_nudge_plan_moves_the_task_and_slides_its_subtasks():
    """The whole plan shifts by one delta, so it keeps its shape and length."""
    parent = make_task("p", deadline=(NOW - timedelta(days=1)).isoformat())
    kid = make_task("k", parent_id="p",
                    deadline=(NOW - timedelta(days=3)).isoformat())
    grandkid = make_task("g", parent_id="k",
                         deadline=(NOW - timedelta(days=4)).isoformat())
    tasks = [parent, kid, grandkid]
    derived = logic.compute(tasks, SETTINGS, now=NOW)

    target = NOW + timedelta(days=2)
    moves = logic.nudge_plan(tasks, derived, "p", target)

    assert logic.parse_dt(moves["p"]) == target
    # delta is +3 days; every user-set deadline underneath moves by exactly that
    assert logic.parse_dt(moves["k"]) == NOW + timedelta(days=0)
    assert logic.parse_dt(moves["g"]) == NOW - timedelta(days=1)
    # ...so the gaps between them — the length of the plan — are unchanged
    assert logic.parse_dt(moves["p"]) - logic.parse_dt(moves["k"]) == timedelta(days=2)


def test_nudge_plan_leaves_auto_deadlines_to_reschedule_themselves():
    parent = make_task("p", deadline=(NOW - timedelta(days=1)).isoformat())
    kid = make_task("k", parent_id="p", estimated_time=30)  # auto deadline
    tasks = [parent, kid]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    moves = logic.nudge_plan(tasks, derived, "p", NOW + timedelta(days=1))
    assert set(moves) == {"p"}


def test_nudge_plan_handles_a_task_with_no_deadline_at_all():
    task = make_task("solo")
    settings = {**SETTINGS, "auto_deadlines": False}
    derived = logic.compute([task], settings, now=NOW)
    moves = logic.nudge_plan([task], derived, "solo", NOW + timedelta(hours=2))
    assert logic.parse_dt(moves["solo"]) == NOW + timedelta(hours=2)


def test_nudge_plan_ignores_an_unknown_task():
    assert logic.nudge_plan([], {}, "nope", NOW) == {}


# ---- how long a task occupies (block size, and what a nudge preserves) ----

def test_length_is_the_buffered_estimate_for_a_leaf():
    tasks = [make_task("a", estimated_time=60,
                       deadline=(NOW + timedelta(days=1)).isoformat())]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    assert derived["a"]["length_min"] == 78          # 60 * 1.3
    assert derived["a"]["raw_length_min"] == 60      # the work inside the tax


def test_length_of_a_container_is_the_work_it_still_holds():
    parent = make_task("p", estimated_time=5,
                       deadline=(NOW + timedelta(days=1)).isoformat())
    kid = make_task("k", parent_id="p", estimated_time=60)
    derived = logic.compute([parent, kid], SETTINGS, now=NOW)
    assert derived["p"]["length_min"] == 78          # the subtask, not the shell


def test_length_falls_back_rather_than_collapsing_to_nothing():
    """An unestimated task still needs a block you can see and click."""
    tasks = [make_task("a", deadline=(NOW + timedelta(days=1)).isoformat())]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    assert derived["a"]["length_min"] == logic.DEFAULT_ESTIMATE_MIN
