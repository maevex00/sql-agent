from datetime import date
from pathlib import Path

import pytest

from compiler.sql_compiler import compile_sql
from joinplanner.sql_builder import SchemaCatalog, plan_join_for_fields
from planner.query_plan import (
    AbsoluteTimeRange,
    Aggregation,
    Filter,
    MetricComparison,
    QueryPlan,
    SortSpec,
)

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


@pytest.fixture(scope="module")
def schema() -> SchemaCatalog:
    return SchemaCatalog(CORRELATION_PATH)


def test_simple_aggregation_single_table(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="unit_cost", func="avg", alias="avg_cost")],
        dimensions=["category_id"],
        limit=10,
    )
    join_plan, field_table = plan_join_for_fields(schema, ["unit_cost", "category_id"])
    sql = compile_sql(plan, join_plan, field_table)

    assert "AVG(T0.unit_cost) AS avg_cost" in sql
    assert "GROUP BY" in sql
    assert "T0.category_id" in sql
    assert "LIMIT 10" in sql


def test_multi_table_join_with_filter_and_sort(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="quantity", func="sum", alias="total_qty")],
        dimensions=["region_name", "category_name"],
        filters=[Filter(field="status", op="=", value="completed")],
        sort=[SortSpec(field="total_qty", direction="desc")],
        limit=20,
    )
    fields = ["quantity", "region_name", "category_name", "status"]
    join_plan, field_table = plan_join_for_fields(schema, fields)
    sql = compile_sql(plan, join_plan, field_table)

    assert "LEFT JOIN" in sql
    assert "= 'completed'" in sql
    assert "ORDER BY" in sql
    assert "total_qty DESC" in sql


def test_having_references_metric_alias_not_raw_column(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="unit_cost", func="sum", alias="total_cost")],
        dimensions=["category_id"],
        having=[Filter(field="total_cost", op=">", value=1000)],
    )
    join_plan, field_table = plan_join_for_fields(schema, ["unit_cost", "category_id"])
    sql = compile_sql(plan, join_plan, field_table)

    # Postgres does not allow SELECT-list aliases in HAVING -- the compiler
    # must re-emit the full aggregate expression, not the bare alias.
    assert "HAVING" in sql
    assert "SUM(T0.unit_cost) > 1000" in sql
    assert "HAVING\n  total_cost" not in sql


def test_metric_comparison_which_regions_missed_target(schema):
    # "Which regions missed their sales targets?" -- SUM(actual) < SUM(target)
    plan = QueryPlan(
        metrics=[
            Aggregation(field="unit_price", func="sum", alias="actual_sales"),
            Aggregation(field="target_amount", func="sum", alias="target_sales"),
        ],
        dimensions=["region_name"],
        metric_comparisons=[
            MetricComparison(
                left=Aggregation(field="unit_price", func="sum", alias="actual_sales"),
                op="<",
                right=Aggregation(field="target_amount", func="sum", alias="target_sales"),
            )
        ],
    )
    fields = ["unit_price", "target_amount", "region_name"]
    join_plan, field_table = plan_join_for_fields(schema, fields)
    sql = compile_sql(plan, join_plan, field_table)

    assert "SUM(" in sql and "<" in sql
    assert "HAVING" in sql


def test_having_unknown_alias_raises(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="unit_cost", func="sum", alias="total_cost")],
        dimensions=["category_id"],
        having=[Filter(field="does_not_exist", op=">", value=1)],
    )
    join_plan, field_table = plan_join_for_fields(schema, ["unit_cost", "category_id"])
    with pytest.raises(ValueError, match="unknown metric alias"):
        compile_sql(plan, join_plan, field_table)


def test_absolute_time_range_becomes_between(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="quantity", func="sum")],
        time_range=AbsoluteTimeRange(field="order_date", start=date(2026, 1, 1), end=date(2026, 3, 31)),
    )
    fields = ["quantity", "order_date"]
    join_plan, field_table = plan_join_for_fields(schema, fields)
    sql = compile_sql(plan, join_plan, field_table)

    assert "BETWEEN '2026-01-01' AND '2026-03-31'" in sql


def test_server_side_limit_cap_overrides_plan_limit(schema):
    plan = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")], limit=9999)
    join_plan, field_table = plan_join_for_fields(schema, ["quantity"])
    sql = compile_sql(plan, join_plan, field_table, max_limit=500)
    assert "LIMIT 500" in sql


def test_string_literal_is_escaped_not_string_formatted(schema):
    plan = QueryPlan(
        metrics=[Aggregation(field="quantity", func="sum")],
        dimensions=["region_name"],
        filters=[Filter(field="region_name", op="=", value="O'Brien Region")],
    )
    fields = ["quantity", "region_name"]
    join_plan, field_table = plan_join_for_fields(schema, fields)
    sql = compile_sql(plan, join_plan, field_table)
    assert "O''Brien Region" in sql  # sqlglot's own quoting, not manual formatting
