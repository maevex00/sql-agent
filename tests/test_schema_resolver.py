from pathlib import Path

import pytest

from joinplanner.sql_builder import SchemaCatalog
from planner.query_plan import Aggregation, Filter, MetricComparison, QueryPlan, SortSpec
from resolver.schema_resolver import apply_field_rewrites, resolve_field_name, resolve_query_plan_fields

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


@pytest.fixture(scope="module")
def schema() -> SchemaCatalog:
    return SchemaCatalog(CORRELATION_PATH)


def test_exact_match(schema):
    res = resolve_field_name("product_name", schema)
    assert res.resolved == "product_name"
    assert res.candidates == []


def test_normalized_match_case_and_spacing(schema):
    res = resolve_field_name("Product Name", schema)
    assert res.resolved == "product_name"


def test_single_fuzzy_match(schema):
    res = resolve_field_name("prodct_name", schema)  # typo
    assert res.resolved == "product_name"
    assert "product_name" in res.candidates


def test_unresolvable_field_returns_none_with_candidates_or_empty(schema):
    res = resolve_field_name("completely_unrelated_xyz", schema)
    assert res.resolved is None


def test_second_single_fuzzy_match_case(schema):
    res = resolve_field_name("regio_name", schema)
    assert res.resolved == "region_name"


def test_ambiguous_fuzzy_match_returns_unresolved_with_multiple_candidates(tmp_path):
    # Two fields scoring EXACTLY the same similarity against the query
    # (verified empirically: SequenceMatcher gives both 0.909) -- the
    # resolver must report both as candidates and leave `resolved` as None
    # rather than picking one arbitrarily.
    correlation = {
        "tables": ["t1", "t2"],
        "table_column": {"t1": ["widget_sizd"], "t2": ["widget_sizf"]},
        "nextarc": {},
    }
    import json

    path = tmp_path / "correlation.json"
    path.write_text(json.dumps(correlation), encoding="utf-8")
    schema = SchemaCatalog(path)

    res = resolve_field_name("widget_size", schema)
    assert res.resolved is None
    assert set(res.candidates) == {"widget_sizd", "widget_sizf"}


def test_resolve_query_plan_fields_full_plan(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="unit_price", func="sum", alias="revenue")],
        dimensions=["Region Name"],  # deliberately off-canonical casing/spacing
        filters=[Filter(field="status", op="=", value="completed")],
        having=[Filter(field="revenue", op=">", value=1000)],  # references metric alias, not a schema field
        sort=[SortSpec(field="revenue", direction="desc")],  # also a metric alias
    )
    result = resolve_query_plan_fields(plan, schema)
    assert result.ok
    assert "unit_price" in result.schema_fields
    assert "region_name" in result.schema_fields
    assert "status" in result.schema_fields
    assert result.field_rewrites == {"Region Name": "region_name"}


def test_resolve_query_plan_fields_reports_unresolved_with_context(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="totally_made_up_field", func="sum")],
        dimensions=[],
    )
    result = resolve_query_plan_fields(plan, schema)
    assert not result.ok
    assert result.unresolved[0].context == "metrics[0].field"


def test_resolve_metric_comparison_fields(schema):
    plan = QueryPlan(
        metrics=[],
        metric_comparisons=[
            MetricComparison(
                left=Aggregation(field="unit_price", func="sum"),
                op="<",
                right=Aggregation(field="target_amount", func="sum"),
            )
        ],
    )
    result = resolve_query_plan_fields(plan, schema)
    assert result.ok
    assert set(result.schema_fields) == {"unit_price", "target_amount"}


def test_apply_field_rewrites_covers_every_location(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="Unit Price", func="sum", alias="revenue")],
        dimensions=["Region Name"],
        filters=[Filter(field="Status", op="=", value="completed")],
    )
    rewrites = {"Unit Price": "unit_price", "Region Name": "region_name", "Status": "status"}
    rewritten = apply_field_rewrites(plan, rewrites)
    assert rewritten.metrics[0].field == "unit_price"
    assert rewritten.dimensions == ["region_name"]
    assert rewritten.filters[0].field == "status"
    # original plan is untouched (deep copy, not mutation)
    assert plan.metrics[0].field == "Unit Price"
