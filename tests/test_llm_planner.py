import pytest
from pydantic import ValidationError

from planner.llm_planner import build_tool_schema, parse_llm_output


def test_build_tool_schema_has_expected_shape():
    tool = build_tool_schema()
    assert tool["name"] == "submit_query_plan"
    assert "input_schema" in tool
    assert tool["input_schema"]["type"] == "object"


def test_parse_valid_output():
    raw = {
        "metrics": [{"field": "unit_price", "func": "sum", "alias": "revenue"}],
        "dimensions": ["region_name"],
        "limit": 50,
    }
    plan = parse_llm_output(raw)
    assert plan.metrics[0].field == "unit_price"
    assert plan.limit == 50


def test_parse_missing_required_field_raises_validation_error():
    raw = {"dimensions": ["region_name"]}  # missing required "metrics"
    with pytest.raises(ValidationError):
        parse_llm_output(raw)


def test_parse_invalid_enum_value_raises_validation_error():
    raw = {"metrics": [{"field": "unit_price", "func": "median"}]}  # not a valid func
    with pytest.raises(ValidationError):
        parse_llm_output(raw)


def test_parse_relative_time_range_discriminated_union():
    raw = {
        "metrics": [{"field": "unit_price", "func": "sum"}],
        "time_range": {"kind": "relative", "field": "order_date", "period": "last_quarter"},
    }
    plan = parse_llm_output(raw)
    assert plan.time_range.kind == "relative"
    assert plan.time_range.period == "last_quarter"
