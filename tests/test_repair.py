import json
from datetime import date
from pathlib import Path

import pytest

from joinplanner.sql_builder import SchemaCatalog
from planner.repair import MAX_ATTEMPTS_PER_TYPE, MAX_TOTAL_REPAIR_CYCLES, run_pipeline

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


@pytest.fixture(scope="module")
def schema() -> SchemaCatalog:
    return SchemaCatalog(CORRELATION_PATH)


def _unreachable_fn(*args, **kwargs):
    raise AssertionError("this repair function should not have been called")


def test_happy_path_no_repairs_needed(schema):
    valid_raw = {
        "metrics": [{"field": "unit_price", "func": "sum", "alias": "revenue"}],
        "dimensions": ["region_name"],
        "filters": [{"field": "status", "op": "=", "value": "completed"}],
        "limit": 25,
    }
    outcome = run_pipeline(
        "revenue by region for completed orders",
        schema,
        planner_fn=lambda q: valid_raw,
        structural_repair_fn=_unreachable_fn,
        schema_resolution_repair_fn=_unreachable_fn,
        semantic_replan_fn=_unreachable_fn,
    )
    assert outcome.ok
    assert outcome.repair_log == []
    assert "SUM(" in outcome.sql
    assert "LEFT JOIN" in outcome.sql


def test_structural_repair_recovers_from_missing_required_field(schema):
    calls = {"n": 0}

    def planner_fn(q):
        return {"dimensions": ["region_name"]}  # missing required "metrics"

    def structural_repair_fn(question, bad_raw, error_msg):
        calls["n"] += 1
        assert "metrics" in error_msg
        return {"metrics": [{"field": "unit_price", "func": "sum"}], "dimensions": ["region_name"]}

    outcome = run_pipeline(
        "revenue by region",
        schema,
        planner_fn=planner_fn,
        structural_repair_fn=structural_repair_fn,
        schema_resolution_repair_fn=_unreachable_fn,
        semantic_replan_fn=_unreachable_fn,
    )
    assert outcome.ok
    assert calls["n"] == 1
    assert len(outcome.repair_log) == 1
    assert "structural" in outcome.repair_log[0]


def test_structural_repair_exhausted_returns_clear_error(schema):
    always_broken = {"dimensions": ["region_name"]}  # never has "metrics"
    outcome = run_pipeline(
        "revenue by region",
        schema,
        planner_fn=lambda q: always_broken,
        structural_repair_fn=lambda q, raw, err: always_broken,
        schema_resolution_repair_fn=_unreachable_fn,
        semantic_replan_fn=_unreachable_fn,
    )
    assert not outcome.ok
    assert "structural repair exhausted" in outcome.error


def test_schema_resolution_repair_picks_from_constrained_candidates(schema):
    raw_with_typo = {
        "metrics": [{"field": "unit_price", "func": "sum"}],
        "dimensions": ["regoin_nam_totally_wrong"],  # unresolvable, not even a fuzzy near-miss
    }

    def schema_resolution_repair_fn(question, unresolved):
        assert unresolved[0].original == "regoin_nam_totally_wrong"
        # repair function must pick only from the field catalog, simulating a
        # constrained LLM choice -- here it "knows" the right answer
        return {"regoin_nam_totally_wrong": "region_name"}

    outcome = run_pipeline(
        "revenue by region",
        schema,
        planner_fn=lambda q: raw_with_typo,
        structural_repair_fn=_unreachable_fn,
        schema_resolution_repair_fn=schema_resolution_repair_fn,
        semantic_replan_fn=_unreachable_fn,
    )
    assert outcome.ok
    assert "region_name" in outcome.sql
    assert len(outcome.repair_log) == 1
    assert "schema-resolution" in outcome.repair_log[0]


def test_schema_resolution_repair_exhausted(schema):
    raw_with_typo = {
        "metrics": [{"field": "unit_price", "func": "sum"}],
        "dimensions": ["nonexistent_field_xyz"],
    }
    outcome = run_pipeline(
        "revenue by region",
        schema,
        planner_fn=lambda q: raw_with_typo,
        structural_repair_fn=_unreachable_fn,
        # keeps returning the same wrong mapping -- never actually fixes it
        schema_resolution_repair_fn=lambda q, u: {"nonexistent_field_xyz": "nonexistent_field_xyz"},
        semantic_replan_fn=_unreachable_fn,
    )
    assert not outcome.ok
    assert "schema-resolution repair exhausted" in outcome.error


def test_semantic_replan_recovers_from_unreachable_join(tmp_path):
    # Two genuinely disconnected tables -- no amount of field-name fixing can
    # resolve this; only dropping one of the fields (a real replan) can.
    correlation = {
        "tables": ["island_a", "island_b"],
        "table_column": {"island_a": ["a_value"], "island_b": ["b_value"]},
        "nextarc": {},
    }
    path = tmp_path / "correlation.json"
    path.write_text(json.dumps(correlation), encoding="utf-8")
    disconnected_schema = SchemaCatalog(path)

    unreachable_raw = {
        "metrics": [{"field": "a_value", "func": "sum"}],
        "dimensions": ["b_value"],
    }
    fixed_raw = {"metrics": [{"field": "a_value", "func": "sum"}], "dimensions": []}

    def semantic_replan_fn(question, prior_plan, reason):
        assert "No JOIN path found" in reason
        return fixed_raw

    outcome = run_pipeline(
        "sum of a and b together",
        disconnected_schema,
        planner_fn=lambda q: unreachable_raw,
        structural_repair_fn=_unreachable_fn,
        schema_resolution_repair_fn=_unreachable_fn,
        semantic_replan_fn=semantic_replan_fn,
    )
    assert outcome.ok
    assert len(outcome.repair_log) == 1
    assert "semantic replan" in outcome.repair_log[0]


def test_relative_time_range_is_resolved_deterministically_not_by_llm(schema):
    raw = {
        "metrics": [{"field": "quantity", "func": "sum"}],
        "time_range": {"kind": "relative", "field": "order_date", "period": "last_quarter"},
    }
    outcome = run_pipeline(
        "quantity last quarter",
        schema,
        planner_fn=lambda q: raw,
        structural_repair_fn=_unreachable_fn,
        schema_resolution_repair_fn=_unreachable_fn,
        semantic_replan_fn=_unreachable_fn,
        today=date(2026, 9, 23),
    )
    assert outcome.ok
    assert outcome.query_plan.time_range.kind == "absolute"
    assert outcome.query_plan.time_range.start == date(2026, 4, 1)
    assert outcome.query_plan.time_range.end == date(2026, 6, 30)
    assert "BETWEEN '2026-04-01' AND '2026-06-30'" in outcome.sql


def test_global_repair_ceiling_caps_total_attempts_across_types(schema):
    """Structural repair burns 2 of the 3 global repair cycles fixing a bad
    metrics list; the field name is fixed at the same time but still needs
    one schema-resolution repair, which would push total repairs to 4 -- over
    MAX_TOTAL_REPAIR_CYCLES (3) even though schema-resolution's own per-type
    budget (2) was never reached.
    """
    assert MAX_ATTEMPTS_PER_TYPE == 2
    assert MAX_TOTAL_REPAIR_CYCLES == 3

    attempts = {"n": 0}

    def planner_fn(q):
        return {"dimensions": ["region_name"]}  # missing "metrics", every time

    def structural_repair_fn(question, bad_raw, err):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return {"dimensions": ["region_name"]}  # still broken
        # fixed on the 2nd structural attempt, but with an unresolvable field
        return {"metrics": [{"field": "still_wrong_field", "func": "sum"}], "dimensions": ["region_name"]}

    outcome = run_pipeline(
        "revenue by region",
        schema,
        planner_fn=planner_fn,
        structural_repair_fn=structural_repair_fn,
        schema_resolution_repair_fn=lambda q, u: {"still_wrong_field": "still_wrong_field"},
        semantic_replan_fn=_unreachable_fn,
    )
    assert not outcome.ok
    assert len(outcome.repair_log) == 3  # 2 structural + 1 schema-resolution, then ceiling hit
