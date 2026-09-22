from pathlib import Path

from planner.query_plan import Aggregation, QueryPlan
from report.analyzer import analyze, describe_result_shape, is_chartable, render_chart


def test_describe_result_shape_empty():
    assert describe_result_shape(["x"], []) == "No rows returned."


def test_describe_result_shape_single_scalar():
    assert describe_result_shape(["total_qty"], [(42,)]) == "total_qty = 42"


def test_describe_result_shape_multi_row():
    desc = describe_result_shape(["region_name", "total_qty"], [("East", 10), ("West", 20)])
    assert desc == "2 rows across 2 columns: region_name, total_qty."


def _plan(n_dimensions: int) -> QueryPlan:
    return QueryPlan(
        metrics=[Aggregation(field="quantity", func="sum", alias="total_qty")],
        dimensions=["region_name", "category_name"][:n_dimensions],
    )


def test_is_chartable_one_dimension_few_rows():
    plan = _plan(n_dimensions=1)
    rows = [("East", 10), ("West", 20)]
    assert is_chartable(plan, ["region_name", "total_qty"], rows)


def test_is_chartable_false_for_no_dimensions():
    plan = _plan(n_dimensions=0)
    assert not is_chartable(plan, ["total_qty"], [(42,)])


def test_is_chartable_false_for_two_dimensions():
    plan = _plan(n_dimensions=2)
    rows = [("East", "Widgets", 10)]
    assert not is_chartable(plan, ["region_name", "category_name", "total_qty"], rows)


def test_is_chartable_false_for_too_many_rows():
    plan = _plan(n_dimensions=1)
    rows = [(f"region_{i}", i) for i in range(30)]
    assert not is_chartable(plan, ["region_name", "total_qty"], rows)


def test_is_chartable_false_for_empty_rows():
    plan = _plan(n_dimensions=1)
    assert not is_chartable(plan, ["region_name", "total_qty"], [])


def test_render_chart_produces_a_real_file(tmp_path):
    plan = QueryPlan(
        metrics=[Aggregation(field="quantity", func="sum", alias="total_qty")],
        dimensions=["region_name"],
    )
    columns = ["region_name", "total_qty"]
    rows = [("East", 10), ("West", 20), ("North", 5)]
    out = render_chart(plan, columns, rows, tmp_path / "chart.png")
    path = Path(out)
    assert path.exists()
    assert path.stat().st_size > 0


def test_analyze_without_llm_chartable_case(tmp_path):
    plan = QueryPlan(
        metrics=[Aggregation(field="quantity", func="sum", alias="total_qty")],
        dimensions=["region_name"],
    )
    columns = ["region_name", "total_qty"]
    rows = [("East", 10), ("West", 20)]

    result = analyze("units by region", plan, columns, rows, chart_dir=tmp_path, use_llm_summary=False)
    assert result.headline == "2 rows across 2 columns: region_name, total_qty."
    assert result.summary == result.headline  # fell back, no LLM call
    assert result.chart_path is not None
    assert Path(result.chart_path).exists()


def test_analyze_without_llm_non_chartable_case(tmp_path):
    plan = QueryPlan(metrics=[Aggregation(field="quantity", func="sum", alias="total_qty")])
    columns = ["total_qty"]
    rows = [(42,)]

    result = analyze("total units", plan, columns, rows, chart_dir=tmp_path, use_llm_summary=False)
    assert result.chart_path is None
    assert result.headline == "total_qty = 42"
