"""Validates eval/benchmark.jsonl itself -- every ground-truth case must be
structurally sound against the REAL schema. This cannot verify a case's
question wording actually elicits its expected_plan (that needs a live LLM),
but it does catch the class of error that's actually preventable here: a
typo'd field name, an expected_plan that references fields that can't
actually be joined, or a malformed JSON row.
"""
from pathlib import Path

import pytest

from joinplanner.sql_builder import SchemaCatalog
from loader import load_benchmark
from scoring import resolved_tables

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"
EXPECTED_CATEGORIES = {
    "simple_aggregation",
    "multi_table_join",
    "time_series",
    "multi_filter",
    "ambiguous_terminology",
    "unsupported_unsafe",
}


@pytest.fixture(scope="module")
def schema() -> SchemaCatalog:
    return SchemaCatalog(CORRELATION_PATH)


@pytest.fixture(scope="module")
def cases():
    return load_benchmark()


def test_benchmark_has_a_reasonable_number_of_cases(cases):
    assert len(cases) >= 50


def test_every_case_id_is_unique(cases):
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


def test_every_case_uses_a_known_category(cases):
    for c in cases:
        assert c.category in EXPECTED_CATEGORIES, f"{c.id} has unknown category {c.category!r}"


def test_each_category_has_at_least_five_cases(cases):
    counts = {cat: 0 for cat in EXPECTED_CATEGORIES}
    for c in cases:
        counts[c.category] += 1
    for cat, n in counts.items():
        assert n >= 5, f"category {cat!r} only has {n} cases"


def test_unsupported_unsafe_cases_have_no_expected_plan(cases):
    for c in cases:
        if c.category == "unsupported_unsafe":
            assert c.expected_intent == "UNSUPPORTED"
            assert c.expected_plan is None


def test_analytics_categories_have_an_expected_plan(cases):
    for c in cases:
        if c.category != "unsupported_unsafe":
            assert c.expected_plan is not None, f"{c.id} is missing expected_plan"
            assert c.expected_intent == "ANALYTICS_QUERY"


def test_every_expected_plan_field_resolves_and_is_joinable(cases, schema):
    """The real integrity check: every field the ground truth references
    must exist in the schema (catches typos) and the join planner must be
    able to connect all of them (catches an accidentally-unreachable
    combination of fields).
    """
    failures = []
    for c in cases:
        if c.expected_plan is None:
            continue
        tables = resolved_tables(c.expected_plan, schema)
        if tables is None:
            failures.append(c.id)
    assert not failures, f"expected_plan not resolvable/joinable for: {failures}"


def test_multi_table_join_cases_actually_span_more_than_one_table(cases, schema):
    failures = []
    for c in cases:
        if c.category != "multi_table_join":
            continue
        tables = resolved_tables(c.expected_plan, schema)
        if tables is None or len(tables) < 2:
            failures.append(c.id)
    assert not failures, f"multi_table_join cases that don't actually need a join: {failures}"
