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


def test_breakdown_caps_step_count(monkeypatch):
    _fake_call(monkeypatch, {"steps": [f"step {i}" for i in range(50)]})
    assert len(ai.breakdown({}, "t", "", 3)) == ai.MAX_STEPS


def test_compile_caps_task_count(monkeypatch):
    _fake_call(monkeypatch, {"tasks": [
        {"title": f"t{i}", "description": ""} for i in range(100)
    ]})
    assert len(ai.compile_braindump({}, "text")) == ai.MAX_COMPILED
