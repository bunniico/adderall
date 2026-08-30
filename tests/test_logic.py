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
    # quick win horizon = 1 day from creation, and inside that day it is put
    # where the working day actually has room: 9am, plus its buffered hour.
    assert d["deadline"] == "2026-08-30T10:18:00+00:00"


def test_horizon_alone_when_spreading_is_off():
    """With the planner off it is the plain horizon it always was."""
    t = make_task("a", estimated_time=60, impact=8, effort=2)
    d = logic.compute([t], {**SETTINGS, "spread_tasks": False}, now=NOW)["a"]
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


# ---- the sorter (how the list is read) ----

def sorter(field, direction=None):
    settings = {**SETTINGS, "sort_field": field}
    if direction:
        settings["sort_dir"] = direction
    return settings


def sorted_ids(tasks, settings, of=None):
    """Top-level ids in the order the page draws them (see sortActive)."""
    derived = logic.compute(tasks, settings, now=NOW)
    rows = of if of is not None else [t for t in tasks if t["parent_id"] is None]
    return [t["id"] for t in sorted(rows, key=lambda t: derived[t["id"]]["list_sort_key"])]


def test_sort_mode_reads_the_stored_choice():
    assert logic.sort_mode({}) == ("smart", True)
    assert logic.sort_mode({"sort_field": "deadline"}) == ("deadline", False)
    assert logic.sort_mode({"sort_field": "deadline", "sort_dir": "desc"}) == ("deadline", True)
    # a list arranged by hand before the sorter existed is still a manual list
    assert logic.sort_mode({"manual_order": True}) == ("manual", False)
    # ...but a field you actually picked wins over that flag
    assert logic.sort_mode({"sort_field": "score", "manual_order": True}) == ("score", True)
    assert logic.sort_mode({"sort_field": "vibes", "sort_dir": "sideways"}) == ("smart", True)


def test_sort_by_score_reads_best_first_and_flips_on_request():
    weak = make_task("weak", order_index=0, impact=1, effort=9, estimated_time=30)
    strong = make_task("strong", order_index=1, impact=9, effort=1, estimated_time=30)
    tasks = [weak, strong]
    assert sorted_ids(tasks, sorter("score")) == ["strong", "weak"]
    assert sorted_ids(tasks, sorter("score", "asc")) == ["weak", "strong"]


def test_sort_by_deadline_sinks_undated_tasks_either_way():
    settings = {**SETTINGS, "auto_deadlines": False, "sort_field": "deadline"}
    soon = make_task("soon", order_index=0,
                     deadline=(NOW + timedelta(hours=2)).isoformat())
    later = make_task("later", order_index=1,
                      deadline=(NOW + timedelta(days=3)).isoformat())
    undated = make_task("undated", order_index=2)
    tasks = [soon, later, undated]
    assert sorted_ids(tasks, settings) == ["soon", "later", "undated"]
    # flipped round it is the furthest deadline first — but a task with no
    # deadline at all is still not the answer to "what is due last?"
    assert sorted_ids(tasks, {**settings, "sort_dir": "desc"}) == [
        "later", "soon", "undated"]


def test_sort_by_deadline_reads_a_container_off_the_work_it_holds():
    settings = {**SETTINGS, "auto_deadlines": False, "sort_field": "deadline"}
    solo = make_task("solo", order_index=0,
                     deadline=(NOW + timedelta(days=2)).isoformat())
    parent = make_task("parent", order_index=1)
    step = make_task("step", parent_id="parent",
                     deadline=(NOW + timedelta(hours=1)).isoformat())
    assert sorted_ids([solo, parent, step], settings) == ["parent", "solo"]


def test_sort_by_subtasks_counts_open_steps_all_the_way_down():
    flat = make_task("flat", order_index=0)
    small = make_task("small", order_index=1)
    small_step = make_task("small_step", parent_id="small")
    big = make_task("big", order_index=2)
    big_step = make_task("big_step", parent_id="big")
    grandsteps = [make_task(f"g{i}", parent_id="big_step", order_index=i)
                  for i in range(2)]
    tasks = [flat, small, small_step, big, big_step, *grandsteps]
    assert sorted_ids(tasks, sorter("subtasks")) == ["big", "small", "flat"]
    assert sorted_ids(tasks, sorter("subtasks", "asc")) == ["flat", "small", "big"]
    derived = logic.compute(tasks, sorter("subtasks"), now=NOW)
    assert derived["big"]["open_subtasks"] == 3   # the step and both grandsteps
    assert derived["flat"]["open_subtasks"] == 0


def test_open_subtasks_counts_only_work_still_to_do():
    parent = make_task("p")
    finished = make_task("finished", parent_id="p", order_index=0, status="done")
    dropped = make_task("dropped", parent_id="p", order_index=1, status="discarded")
    inside_dropped = make_task("inside_dropped", parent_id="dropped")
    todo = make_task("todo", parent_id="p", order_index=2)
    derived = logic.compute([parent, finished, dropped, inside_dropped, todo],
                            SETTINGS, now=NOW)
    assert derived["p"]["open_subtasks"] == 1


def test_sort_by_created_reads_newest_or_oldest_first():
    old = make_task("old", order_index=1,
                    created_at=(NOW - timedelta(days=2)).isoformat())
    new = make_task("new", order_index=0, created_at=NOW.isoformat())
    tasks = [old, new]
    assert sorted_ids(tasks, sorter("created")) == ["new", "old"]
    assert sorted_ids(tasks, sorter("created", "asc")) == ["old", "new"]


def test_sorting_the_list_leaves_what_next_alone():
    """The sorter is a lens on the list, not a change of plan."""
    urgent = make_task("urgent", order_index=1, estimated_time=60,
                       deadline=(NOW + timedelta(minutes=70)).isoformat())
    calm = make_task("calm", order_index=0, impact=8, effort=2, estimated_time=10,
                     created_at=(NOW - timedelta(days=1)).isoformat())
    tasks = [urgent, calm]
    settings = {**sorter("created", "asc"), "auto_deadlines": False}
    assert sorted_ids(tasks, settings) == ["calm", "urgent"]
    derived = logic.compute(tasks, settings, now=NOW)
    assert logic.next_task(tasks, derived)["id"] == "urgent"


def test_manual_stays_manual_through_the_sorter():
    first = make_task("first", order_index=0)
    second = make_task("second", order_index=1, estimated_time=10,
                       deadline=(NOW + timedelta(minutes=11)).isoformat())
    tasks = [first, second]
    assert sorted_ids(tasks, sorter("manual")) == ["first", "second"]
    derived = logic.compute(tasks, sorter("manual"), now=NOW)
    for t in tasks:
        assert derived[t["id"]]["list_sort_key"] == derived[t["id"]]["sort_key"]


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


def test_brevity_decays_with_the_clock():
    """Time cost, inverted onto the same 0-10 scale as impact and effort."""
    assert logic.brevity(logic.TIME_COST_HALFLIFE) == 5.0
    assert logic.brevity(10) > logic.brevity(60) > logic.brevity(480)
    assert 0 < logic.brevity(10_000) < 1        # saturates, never goes negative
    # An un-estimated task is unknown, not quick: guessing otherwise would
    # reward never estimating anything.
    assert logic.brevity(None) == logic.NEUTRAL_SCORE
    assert logic.brevity(0) == logic.NEUTRAL_SCORE
    # The curve is a decay, so the same twenty minutes matters more to a short
    # task than to a long one.
    assert (logic.brevity(10) - logic.brevity(30)) > (logic.brevity(340) - logic.brevity(360))


def test_priority_score_counts_time_cost_apart_from_effort():
    """Two tasks equally unpleasant, one of them three hours long.

    Effort is how hard a task is to face and time is how much of the day it
    eats; a score built on effort alone calls a two-minute chore and an
    afternoon of the same drudgery the same task.
    """
    quick = logic.priority_score(6, 4, 5.0, minutes=10)
    slog = logic.priority_score(6, 4, 5.0, minutes=180)
    assert quick > slog
    # Unknown length lands between the two rather than at either end.
    assert quick > logic.priority_score(6, 4, 5.0) > slog
    # Time is a tie-breaker, not the story: a long, urgent, important task
    # still outranks a quick, slack, pointless one.
    assert logic.priority_score(9, 3, 10.0, minutes=480) > \
        logic.priority_score(1, 8, 0.5, minutes=5)


def test_priority_score_stays_on_the_0_100_scale():
    best = logic.priority_score(10, 0, 10.0, minutes=1)
    worst = logic.priority_score(0, 10, 0.0, minutes=100_000)
    assert best <= 100 and worst >= 0 and best > worst


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


def test_container_urgency_reads_the_work_it_still_holds():
    """A parent's urgency follows the work in its subtree, not its own estimate.

    An hour away with five minutes of its own work looks relaxed; an hour away
    with an hour of subtasks under it does not, and the number has to say so.
    """
    parent = make_task("p", impact=8, effort=3, estimated_time=5,
                       deadline=(NOW + timedelta(minutes=60)).isoformat())
    kid = make_task("k", parent_id="p", estimated_time=60)
    derived = logic.compute([parent, kid], SETTINGS, now=NOW)
    assert derived["p"]["urgency"] == 10.0        # 78m of work, 60m left
    assert derived["p"]["length_min"] == derived["k"]["length_min"]


# ---- a parent is its parts ----

def test_container_score_is_inherited_from_its_subtasks():
    """A parent has no score of its own — it is worth what is under it.

    Whatever impact and effort someone put on the container describes nothing
    you can sit down and do, so it must not sway the number.
    """
    parent = make_task("p", impact=0, effort=10, estimated_time=5)
    kid = make_task("k", parent_id="p", impact=9, effort=1, estimated_time=20)
    derived = logic.compute([parent, kid], SETTINGS, now=NOW)
    assert derived["p"]["score"] == derived["k"]["score"]
    # Re-rating the container changes nothing; re-rating the step changes it.
    parent["impact"], parent["effort"] = 10, 0
    assert logic.compute([parent, kid], SETTINGS, now=NOW)["p"]["score"] == \
        derived["p"]["score"]


def test_container_score_combines_children_by_the_time_they_cost():
    """Every leaf pulls its weight in minutes, so the score reads as what the
    remaining work is actually worth rather than a headcount of subtasks."""
    parent = make_task("p")
    big = make_task("big", parent_id="p", impact=9, effort=2, estimated_time=180)
    small = make_task("small", parent_id="p", impact=1, effort=9,
                      estimated_time=10, order_index=1)
    derived = logic.compute([parent, big, small], SETTINGS, now=NOW)
    lo, hi = sorted((derived["big"]["score"], derived["small"]["score"]))
    assert lo < derived["p"]["score"] < hi
    # Three hours of the good work against ten minutes of the bad: the mean
    # sits far nearer the work the hours actually go into.
    assert abs(derived["p"]["score"] - derived["big"]["score"]) < \
        abs(derived["p"]["score"] - derived["small"]["score"])


def test_container_score_rolls_up_through_every_level():
    """Grandparents inherit too, and a container adds no minutes of its own —
    counting the shell as well as its steps would weigh the plan twice."""
    root = make_task("root", impact=0, effort=10, estimated_time=999)
    mid = make_task("mid", parent_id="root", impact=0, effort=10, estimated_time=999)
    leaf = make_task("leaf", parent_id="mid", impact=8, effort=2, estimated_time=30)
    derived = logic.compute([root, mid, leaf], SETTINGS, now=NOW)
    assert derived["root"]["score"] == derived["mid"]["score"] == derived["leaf"]["score"]


def test_finished_and_dropped_subtasks_stop_counting():
    """A project is worth what is left of it, not what it once was."""
    parent = make_task("p")
    done = make_task("done", parent_id="p", impact=0, effort=10,
                     estimated_time=200, status="done")
    dropped = make_task("dropped", parent_id="p", impact=0, effort=10,
                        estimated_time=200, status="discarded", order_index=1)
    live = make_task("live", parent_id="p", impact=9, effort=1,
                     estimated_time=30, order_index=2)
    derived = logic.compute([parent, done, dropped, live], SETTINGS, now=NOW)
    assert derived["p"]["score"] == derived["live"]["score"]


def test_container_with_nothing_left_keeps_its_own_score():
    """Everything underneath is finished: there is nothing to inherit from,
    so the container falls back to its own number instead of a zero."""
    parent = make_task("p", impact=8, effort=2, estimated_time=30)
    kid = make_task("k", parent_id="p", estimated_time=30, status="done")
    derived = logic.compute([parent, kid], SETTINGS, now=NOW)
    assert derived["p"]["score"] == logic.priority_score(
        8, 2, derived["p"]["urgency"], derived["p"]["length_min"])


def test_container_score_stays_on_the_0_100_scale():
    parent = make_task("p")
    kids = [make_task(f"k{i}", parent_id="p", impact=i, effort=10 - i,
                      estimated_time=10 * (i + 1), order_index=i)
            for i in range(5)]
    derived = logic.compute([parent, *kids], SETTINGS, now=NOW)
    assert 0 <= derived["p"]["score"] <= 100


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


# ---- spreading work over the days that have room ----

def spans(derived, ids):
    """(start, end) of each task's calendar block, in order."""
    out = []
    for tid in ids:
        end = logic.parse_dt(derived[tid]["deadline"])
        out.append((end - timedelta(minutes=derived[tid]["length_min"]), end))
    return out


def test_same_shaped_tasks_spread_instead_of_stacking():
    """The braindump case: everything created in one minute, all alike.

    They used to land on the same instant of the same afternoon. Now they
    queue up through the working day and roll onto the next one when the day
    is full.
    """
    tasks = [make_task(f"t{i}", estimated_time=120, impact=8, effort=2)
             for i in range(4)]
    derived = logic.compute(tasks, SETTINGS, now=NOW)
    blocks = spans(derived, [t["id"] for t in tasks])

    # Nothing overlaps anything else.
    for (_, first_end), (second_start, _) in zip(blocks, blocks[1:]):
        assert second_start >= first_end

    days = [start.date().isoformat() for start, _ in blocks]
    # 3 × 156 buffered minutes fits inside an eight-hour day; the fourth does not.
    assert days == ["2026-08-30"] * 3 + ["2026-08-31"]
    assert blocks[0][0].isoformat() == "2026-08-30T09:00:00+00:00"


def test_placement_respects_the_daily_cap():
    """A smaller cap fills fewer tasks into a day, and says so."""
    tasks = [make_task(f"t{i}", estimated_time=120, impact=8, effort=2)
             for i in range(4)]
    derived = logic.compute(tasks, {**SETTINGS, "day_capacity": 180}, now=NOW)
    days = {t["id"]: logic.parse_dt(derived[t["id"]]["deadline"]).date().isoformat()
            for t in tasks}
    # 156 minutes each, three hours a day: one task per day, four days running.
    assert sorted(days.values()) == ["2026-08-30", "2026-08-31",
                                     "2026-09-01", "2026-09-02"]


def test_a_deadline_you_set_takes_its_room_first():
    """Auto-placed work fits around commitments, not on top of them."""
    fixed = make_task("fixed", estimated_time=240,   # 312 buffered, ends 13:00
                      deadline="2026-08-30T13:00:00+00:00")
    auto = make_task("auto", estimated_time=60, impact=8, effort=2, order_index=1)
    derived = logic.compute([fixed, auto], SETTINGS, now=NOW)
    (_, fixed_end), (auto_start, _) = spans(derived, ["fixed", "auto"])
    assert fixed_end.isoformat() == "2026-08-30T13:00:00+00:00"
    assert auto_start >= fixed_end


def test_placement_is_stable_across_recomputes():
    """A reload must not reshuffle your week."""
    tasks = [make_task(f"t{i}", estimated_time=90, impact=8, effort=2)
             for i in range(5)]
    first = logic.compute(tasks, SETTINGS, now=NOW)
    second = logic.compute(tasks, SETTINGS, now=NOW)
    assert {k: v["deadline"] for k, v in first.items()} == \
           {k: v["deadline"] for k, v in second.items()}


def test_a_tree_is_booked_once_not_once_per_step():
    """A container and its steps are the same work, not twice the work."""
    parent = make_task("p", impact=8, effort=2)
    kids = [make_task(f"c{i}", parent_id="p", estimated_time=120, order_index=i)
            for i in range(2)]
    other = make_task("other", estimated_time=60, impact=8, effort=2, order_index=1)
    derived = logic.compute([parent, *kids, other], SETTINGS, now=NOW)

    # The tree is one span: its last step ends exactly on the parent's deadline
    # and its first starts one subtree's worth of work earlier.
    parent_end = logic.parse_dt(derived["p"]["deadline"])
    assert logic.parse_dt(derived["c1"]["deadline"]) == parent_end
    assert derived["p"]["length_min"] == 2 * 156
    assert (parent_end - timedelta(minutes=2 * 156)).isoformat() == \
        "2026-08-30T09:00:00+00:00"
    # 312 minutes of tree plus 78 of the other task is inside the eight-hour
    # cap, so both fit on the same day. Charging the day for the container as
    # well as the steps it is made of would have come to 702 and pushed the
    # second task into tomorrow for no reason.
    assert logic.parse_dt(derived["other"]["deadline"]).date().isoformat() == \
        "2026-08-30"


def test_work_bigger_than_a_day_gets_a_day_to_itself():
    small = make_task("small", estimated_time=60, impact=8, effort=2)
    huge = make_task("huge", estimated_time=720, impact=8, effort=2, order_index=1)
    derived = logic.compute([small, huge], SETTINGS, now=NOW)
    # It cannot fit under the cap anywhere, so it takes the first day nothing
    # else has claimed and runs past the end of the working window rather than
    # being cut up or quietly hidden.
    assert derived["huge"]["length_min"] == 936
    start, _ = spans(derived, ["huge"])[0]
    assert start.isoformat() == "2026-08-31T09:00:00+00:00"


def test_today_is_never_scheduled_in_the_hours_already_gone():
    """A slot at 9am is no use at noon."""
    yesterday = (NOW - timedelta(days=1)).isoformat()
    t = make_task("a", estimated_time=60, impact=8, effort=2, created_at=yesterday)
    derived = logic.compute([t], SETTINGS, now=NOW)
    start, _ = spans(derived, ["a"])[0]
    assert start >= NOW


def test_overdue_work_stays_overdue():
    """Spreading must not quietly reschedule the past into the future."""
    long_ago = (NOW - timedelta(days=10)).isoformat()
    t = make_task("a", estimated_time=60, impact=8, effort=2, created_at=long_ago)
    derived = logic.compute([t], SETTINGS, now=NOW)
    assert logic.parse_dt(derived["a"]["deadline"]) < NOW
    assert derived["a"]["urgency"] == 10.0


def test_planner_shared_across_projects_keeps_them_off_each_other():
    """Two tabs, one day: the second project plans around the first."""
    planner = logic.day_planner(SETTINGS)
    work = [make_task("w", estimated_time=300, impact=8, effort=2)]
    house = [make_task("h", estimated_time=300, impact=8, effort=2)]
    first = logic.compute(work, SETTINGS, now=NOW, planner=planner)
    second = logic.compute(house, SETTINGS, now=NOW, planner=planner)
    assert logic.parse_dt(first["w"]["deadline"]).date().isoformat() == "2026-08-30"
    # 390 buffered minutes each: the second one cannot also fit in eight hours.
    assert logic.parse_dt(second["h"]["deadline"]).date().isoformat() == "2026-08-31"


def test_reserve_fixed_books_commitments_before_anything_is_placed():
    planner = logic.day_planner(SETTINGS)
    pinned = [make_task("p", estimated_time=300,
                        deadline="2026-08-30T16:00:00+00:00")]
    logic.reserve_fixed(planner, pinned, SETTINGS)
    auto = [make_task("a", estimated_time=300, impact=8, effort=2)]
    derived = logic.compute(auto, SETTINGS, now=NOW, planner=planner)
    # The pinned task ate most of the 30th, so the placed one moves on.
    assert logic.parse_dt(derived["a"]["deadline"]).date().isoformat() == "2026-08-31"


# ---- the daily cap, and what it learns ----

def day_history(pairs):
    """[(days ago, minutes), ...] as completion history rows."""
    return [{"finished_at": (NOW - timedelta(days=d)).isoformat(), "minutes": m}
            for d, m in pairs]


def test_daily_totals_add_up_per_local_day():
    history = day_history([(1, 120), (1, 60), (2, 200)])
    totals = logic.daily_totals(history)
    assert sorted(totals.values()) == [180, 200]


def test_daily_totals_are_local_days():
    """A task finished at 11pm belongs to the day it read as on your wall."""
    history = [{"finished_at": "2026-08-30T02:00:00+00:00", "minutes": 60}]
    ny = logic.resolve_tz("America/New_York")
    assert list(logic.daily_totals(history, ny)) == ["2026-08-29"]
    assert list(logic.daily_totals(history)) == ["2026-08-30"]


def test_capacity_is_the_configured_cap_without_evidence():
    plan = logic.capacity_plan({"day_capacity": 480}, day_history([(1, 60)]))
    assert plan["minutes"] == 480
    assert plan["learned"] is False
    assert plan["days"] == 1  # not enough days to say anything yet


def test_capacity_falls_toward_the_day_you_actually_have():
    """A goal you never reach is not a plan, it is a daily notification."""
    history = day_history([(d, 180) for d in range(1, 9)])
    plan = logic.capacity_plan({"day_capacity": 480}, history)
    assert plan["hit_rate"] == 0.0
    assert plan["typical"] == 180
    assert plan["minutes"] == 330      # halfway from 480 to 180
    assert plan["learned"] is True


def test_capacity_rises_when_the_goal_is_cleared_every_day():
    history = day_history([(d, 600) for d in range(1, 9)])
    plan = logic.capacity_plan({"day_capacity": 480}, history)
    assert plan["hit_rate"] == 1.0
    assert plan["minutes"] == 540      # halfway from 480 to 600


def test_capacity_left_alone_while_the_goal_still_means_something():
    history = day_history([(1, 500), (2, 200), (3, 520), (4, 180),
                           (5, 300), (6, 490)])
    plan = logic.capacity_plan({"day_capacity": 480}, history)
    assert 0.30 <= plan["hit_rate"] <= 0.75
    assert plan["minutes"] == 480
    assert plan["learned"] is False


def test_capacity_learning_can_be_turned_off():
    history = day_history([(d, 120) for d in range(1, 9)])
    plan = logic.capacity_plan(
        {"day_capacity": 480, "adaptive_capacity": False}, history)
    assert plan["minutes"] == 480
    assert plan["adaptive"] is False


def test_capacity_stays_within_its_bounds():
    tiny = logic.capacity_plan({"day_capacity": 5}, [])
    assert tiny["minutes"] == logic.CAPACITY_FLOOR
    huge = logic.capacity_plan({"day_capacity": 5000}, [])
    assert huge["minutes"] == logic.CAPACITY_CEILING


def test_days_off_are_not_evidence_of_a_short_day():
    """Only days you finished something on count."""
    history = day_history([(1, 480), (3, 480), (5, 480), (9, 480), (12, 480)])
    plan = logic.capacity_plan({"day_capacity": 480}, history)
    assert plan["days"] == 5          # five days, not the fortnight between them
    assert plan["typical"] == 480


def test_unknown_timezone_falls_back_to_utc():
    assert logic.resolve_tz("Mars/Olympus") is timezone.utc
    assert logic.resolve_tz("") is timezone.utc
    assert logic.resolve_tz(None) is timezone.utc


# ---- XP and levels ----

def test_task_xp_is_the_score_rounded():
    assert logic.task_xp(62.4) == 62
    assert logic.task_xp(62.6) == 63
    assert logic.task_xp(0.0) == 1      # doing something always counts for something
    assert logic.task_xp(100.0) == 100


def test_level_thresholds_get_steadily_further_apart():
    assert logic.level_threshold(1) == 0
    assert logic.level_threshold(2) == 100
    assert logic.level_threshold(3) == 300
    assert logic.level_threshold(4) == 600
    spans = [logic.level_threshold(n + 1) - logic.level_threshold(n)
             for n in range(1, 6)]
    assert spans == sorted(spans) and len(set(spans)) == len(spans)


def test_level_progress_reads_a_running_total():
    assert logic.level_progress(0) == {
        "total": 0, "level": 1, "into_level": 0, "level_span": 100,
        "to_next": 100, "progress": 0.0,
    }
    # One XP short of the next level is still the current one.
    assert logic.level_progress(99)["level"] == 1
    assert logic.level_progress(100)["level"] == 2
    assert logic.level_progress(299)["level"] == 2
    assert logic.level_progress(300)["level"] == 3

    mid = logic.level_progress(200)
    assert mid["level"] == 2
    assert (mid["into_level"], mid["level_span"], mid["to_next"]) == (100, 200, 100)
    assert mid["progress"] == 0.5


def test_level_progress_never_goes_backwards_or_negative():
    assert logic.level_progress(-50)["level"] == 1
    assert logic.level_progress(-50)["total"] == 0
    levels = [logic.level_progress(n)["level"] for n in range(0, 2000, 37)]
    assert levels == sorted(levels)
