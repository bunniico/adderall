"""Structured-output schemas must stay inside the supported JSON Schema subset.

The API rejects numeric bounds and array-length constraints in
output_config.format.schema with a 400, so every bound is enforced in code
after parsing instead. These tests keep both halves honest.
"""

import pytest

from app import ai

# Rejected by output_config.format.schema (raw schemas are not sanitized by
# the SDK the way Pydantic models are).
UNSUPPORTED = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
}


def walk(node, path="root"):
    """Yield (path, key) for every key in a nested schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk(item, f"{path}[{i}]")


ALL_SCHEMAS = [
    ("breakdown", ai.BREAKDOWN_SCHEMA),
    ("compile", ai.COMPILE_SCHEMA),
    ("annotate_with_scores", ai.annotate_schema(True)),
    ("annotate_no_scores", ai.annotate_schema(False)),
    ("annotate_with_start", ai.annotate_schema(True, want_start=True)),
]


@pytest.mark.parametrize("name,schema", ALL_SCHEMAS)
def test_no_unsupported_keywords(name, schema):
    found = [(path, key) for path, key in walk(schema) if key in UNSUPPORTED]
    assert not found, f"{name} carries unsupported schema keywords: {found}"


@pytest.mark.parametrize("name,schema", ALL_SCHEMAS)
def test_objects_disallow_additional_properties(name, schema):
    """Required by structured outputs for every object in the schema."""
    def check(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, \
                    f"{name}: object at {path} must set additionalProperties: false"
            for key, value in node.items():
                check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                check(item, f"{path}[{i}]")
    check(schema, "root")


def test_annotate_schema_omits_scores_when_disabled():
    props = ai.annotate_schema(False)["properties"]["tasks"]["items"]["properties"]
    assert set(props) == {"id", "minutes"}


def test_annotate_schema_asks_for_a_start_offset_only_when_wanted():
    item = ai.annotate_schema(True, want_start=True)["properties"]["tasks"]["items"]
    assert "start_in_minutes" in item["properties"]
    assert "start_in_minutes" in item["required"]
    # Minutes from now, never a date: no timezone or format for the model to
    # get wrong, and no bound the schema is not allowed to carry.
    assert item["properties"]["start_in_minutes"] == {"type": "integer"}
    plain = ai.annotate_schema(True)["properties"]["tasks"]["items"]["properties"]
    assert "start_in_minutes" not in plain


# ---- the bounds the schema can no longer express are enforced in code ----

def test_clamp_bounds_and_bad_values():
    assert ai._clamp(5, 0, 10) == 5
    assert ai._clamp(99, 0, 10) == 10
    assert ai._clamp(-4, 0, 10) == 0
    assert ai._clamp("7", 0, 10) == 7      # stringified numbers coerce
    assert ai._clamp(None, 0, 10) == 0     # missing falls back to the low bound
    assert ai._clamp("nonsense", 1, 10) == 1


def _fake_call(monkeypatch, payload):
    monkeypatch.setattr(ai, "_call", lambda *a, **kw: payload)


def test_annotate_clamps_out_of_range_model_output(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"id": "a", "minutes": -5, "impact": 42, "effort": -1},
    ]})
    got = ai.annotate({}, [{"id": "a", "title": "t"}])
    assert got["a"] == {"id": "a", "minutes": 1, "impact": 10, "effort": 0}


def test_annotate_drops_unknown_ids(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"id": "real", "minutes": 30, "impact": 5, "effort": 5},
        {"id": "hallucinated", "minutes": 10, "impact": 5, "effort": 5},
    ]})
    got = ai.annotate({}, [{"id": "real", "title": "t"}])
    assert set(got) == {"real"}


def test_annotate_without_scores_returns_minutes_only(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [{"id": "a", "minutes": 20}]})
    got = ai.annotate({}, [{"id": "a", "title": "t"}], want_scores=False)
    assert got["a"] == {"id": "a", "minutes": 20}


def test_annotate_clamps_a_start_offset_into_range(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"id": "a", "minutes": 30, "impact": 5, "effort": 5,
         "start_in_minutes": -90},
        {"id": "b", "minutes": 30, "impact": 5, "effort": 5,
         "start_in_minutes": 10 ** 9},
    ]})
    got = ai.annotate({}, [{"id": "a", "title": "t"}, {"id": "b", "title": "u"}],
                      want_start=True, now_local="Monday 31 August 2026, 14:00")
    assert got["a"]["start_in_minutes"] == 0                    # never the past
    assert got["b"]["start_in_minutes"] == ai.MAX_START_MINUTES  # never past a year


def test_annotate_ignores_a_start_offset_nobody_asked_for(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"id": "a", "minutes": 30, "impact": 5, "effort": 5,
         "start_in_minutes": 120},
    ]})
    got = ai.annotate({}, [{"id": "a", "title": "t"}])
    assert "start_in_minutes" not in got["a"]


def test_annotate_will_not_ask_for_a_start_without_a_clock_to_read(monkeypatch):
    """"In two hours" is only meaningful if the model was told what hour it
    is, so a missing local time turns the whole request off rather than
    inviting a guess."""
    seen = {}

    def fake(settings, tier, prompt, schema, **kw):
        seen["schema"] = schema
        return {"tasks": [{"id": "a", "minutes": 30, "impact": 5, "effort": 5}]}

    monkeypatch.setattr(ai, "_call", fake)
    ai.annotate({}, [{"id": "a", "title": "t"}], want_start=True, now_local="")
    item = seen["schema"]["properties"]["tasks"]["items"]["properties"]
    assert "start_in_minutes" not in item


def test_annotate_tells_the_model_what_time_it_is(monkeypatch):
    seen = {}

    def fake(settings, tier, prompt, schema, **kw):
        seen["prompt"] = prompt
        return {"tasks": []}

    monkeypatch.setattr(ai, "_call", fake)
    ai.annotate({}, [{"id": "a", "title": "eat dinner"}], want_start=True,
                now_local="Monday 31 August 2026, 14:00 (BST)")
    assert "Monday 31 August 2026, 14:00 (BST)" in seen["prompt"]
    assert "start_in_minutes" in seen["prompt"]


def test_breakdown_caps_step_count(monkeypatch):
    _fake_call(monkeypatch, {"steps": [f"step {i}" for i in range(50)]})
    assert len(ai.breakdown({}, "t", "", 3)) == ai.MAX_STEPS


def test_compile_caps_task_count(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"title": f"t{i}", "description": ""} for i in range(100)
    ]})
    assert len(ai.compile_braindump({}, "text")) == ai.MAX_COMPILED


# ---- nesting: the compiled braindump is a tree, bounded in code ----

def test_compile_schema_nests_to_the_declared_depth():
    node = ai.COMPILE_SCHEMA["properties"]["tasks"]["items"]
    for _ in range(ai.COMPILE_DEPTH - 1):
        assert "subtasks" in node["properties"], "nesting stops too early"
        assert "subtasks" in node["required"]
        node = node["properties"]["subtasks"]["items"]
    # The deepest level is a leaf, which is what bounds the recursion.
    assert "subtasks" not in node["properties"]


def test_compile_keeps_the_tree_shape(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"title": "buy gift", "description": "for mom", "subtasks": [
            {"title": "pick a present", "description": "", "subtasks": []},
        ]},
    ]})
    tasks = ai.compile_braindump({}, "text")
    assert tasks == [{
        "title": "buy gift", "description": "for mom",
        "subtasks": [{"title": "pick a present", "description": "", "subtasks": []}],
    }]


def test_compile_truncates_nesting_past_max_depth(monkeypatch):
    deepest = {"title": "too deep", "description": ""}
    node = deepest
    for i in range(ai.COMPILE_DEPTH):
        node = {"title": f"level {i}", "description": "", "subtasks": [node]}
    _fake_call(monkeypatch, {"tasks": [node]})

    task = ai.compile_braindump({}, "text")[0]
    depth = 1
    while task["subtasks"]:
        task = task["subtasks"][0]
        depth += 1
    assert depth == ai.COMPILE_DEPTH
    assert task["title"] != "too deep"


def test_compile_cap_counts_subtasks(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"title": f"root {i}", "description": "", "subtasks": [
            {"title": f"child {i}.{j}", "description": "", "subtasks": []}
            for j in range(4)
        ]}
        for i in range(100)
    ]})

    def count(nodes):
        return sum(1 + count(n["subtasks"]) for n in nodes)

    assert count(ai.compile_braindump({}, "text")) == ai.MAX_COMPILED


def test_compile_drops_untitled_tasks_at_every_level(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"title": "   ", "description": "no title, dropped"},
        {"title": "keep me", "description": "", "subtasks": [
            {"title": "", "description": "", "subtasks": []},
            {"title": "keep me too", "description": "", "subtasks": []},
        ]},
    ]})
    tasks = ai.compile_braindump({}, "text")
    assert [t["title"] for t in tasks] == ["keep me"]
    assert [t["title"] for t in tasks[0]["subtasks"]] == ["keep me too"]


def test_compile_tolerates_a_missing_subtasks_key(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [{"title": "flat", "description": ""}]})
    assert ai.compile_braindump({}, "text") == [
        {"title": "flat", "description": "", "subtasks": []}
    ]
