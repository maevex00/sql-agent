from pathlib import Path

import pytest

from joinplanner.sql_builder import SchemaCatalog
from planner.query_plan import AbsoluteTimeRange, Aggregation, Filter, QueryPlan, RelativeTimeRange
from scoring import (
    correct_join_path,
    dimension_accuracy,
    filter_accuracy,
    metric_accuracy,
    resolved_tables,
    rows_match,
    time_range_accuracy,
)

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


@pytest.fixture(scope="module")
def schema() -> SchemaCatalog:
    return SchemaCatalog(CORRELATION_PATH)


def test_metric_accuracy_exact_match():
    a = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")])
    assert metric_accuracy(a, a) == 1.0


def test_metric_accuracy_partial_overlap():
    actual = QueryPlan(metrics=[Aggregation(field="quantity", func="sum"), Aggregation(field="unit_price", func="avg")])
    expected = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")])
    # intersection={quantity,sum}, union has 2 -> 1/2
    assert metric_accuracy(actual, expected) == 0.5


def test_metric_accuracy_no_overlap():
    actual = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")])
    expected = QueryPlan(metrics=[Aggregation(field="rating", func="avg")])
    assert metric_accuracy(actual, expected) == 0.0


def test_dimension_accuracy_exact_and_partial():
    expected = QueryPlan(metrics=[], dimensions=["region_name", "category_name"])
    exact = QueryPlan(metrics=[], dimensions=["category_name", "region_name"])  # order shouldn't matter
    partial = QueryPlan(metrics=[], dimensions=["region_name"])
    assert dimension_accuracy(exact, expected) == 1.0
    assert dimension_accuracy(partial, expected) == 0.5


def test_filter_accuracy_value_must_match_not_just_field_and_op():
    expected = QueryPlan(metrics=[], filters=[Filter(field="status", op="=", value="completed")])
    wrong_value = QueryPlan(metrics=[], filters=[Filter(field="status", op="=", value="cancelled")])
    assert filter_accuracy(wrong_value, expected) == 0.0


def test_time_range_accuracy_both_none():
    plan = QueryPlan(metrics=[])
    assert time_range_accuracy(plan, plan) == 1.0


def test_time_range_accuracy_relative_period_match():
    expected = QueryPlan(metrics=[], time_range=RelativeTimeRange(field="order_date", period="last_quarter"))
    actual = QueryPlan(metrics=[], time_range=RelativeTimeRange(field="order_date", period="last_quarter"))
    assert time_range_accuracy(actual, expected) == 1.0


def test_time_range_accuracy_relative_period_mismatch():
    expected = QueryPlan(metrics=[], time_range=RelativeTimeRange(field="order_date", period="last_quarter"))
    actual = QueryPlan(metrics=[], time_range=RelativeTimeRange(field="order_date", period="last_month"))
    assert time_range_accuracy(actual, expected) == 0.0


def test_time_range_accuracy_kind_mismatch():
    expected = QueryPlan(metrics=[], time_range=RelativeTimeRange(field="order_date", period="last_quarter"))
    from datetime import date

    actual = QueryPlan(
        metrics=[], time_range=AbsoluteTimeRange(field="order_date", start=date(2026, 1, 1), end=date(2026, 3, 31))
    )
    assert time_range_accuracy(actual, expected) == 0.0


def test_time_range_accuracy_expected_none_but_actual_has_one():
    expected = QueryPlan(metrics=[])
    actual = QueryPlan(metrics=[], time_range=RelativeTimeRange(field="order_date", period="last_quarter"))
    assert time_range_accuracy(actual, expected) == 0.0


def test_resolved_tables_single_table(schema):
    plan = QueryPlan(metrics=[Aggregation(field="unit_cost", func="avg")])
    assert resolved_tables(plan, schema) == frozenset({"products"})


def test_resolved_tables_multi_hop(schema):
    plan = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")], dimensions=["region_name"])
    tables = resolved_tables(plan, schema)
    assert tables == frozenset({"order_items", "orders", "customers", "regions"})


def test_resolved_tables_none_for_unresolvable_field(schema):
    plan = QueryPlan(metrics=[Aggregation(field="not_a_real_field", func="sum")])
    assert resolved_tables(plan, schema) is None


def test_correct_join_path_true_for_identical_plans(schema):
    plan = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")], dimensions=["region_name"])
    assert correct_join_path(plan, plan, schema)


def test_correct_join_path_false_when_actual_touches_fewer_tables(schema):
    expected = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")], dimensions=["region_name"])
    actual = QueryPlan(metrics=[Aggregation(field="quantity", func="sum")])  # missing the region_name dimension
    assert not correct_join_path(actual, expected, schema)


def test_rows_match_ignores_order():
    a = [(1, "x"), (2, "y")]
    b = [(2, "y"), (1, "x")]
    assert rows_match(a, b)


def test_rows_match_false_for_different_rows():
    assert not rows_match([(1, "x")], [(1, "y")])
