import pytest
from pydantic import ValidationError

from router.intent_router import build_tool_schema, parse_intent_output


def test_build_tool_schema_shape():
    tool = build_tool_schema()
    assert tool["name"] == "classify_intent"
    assert "input_schema" in tool


@pytest.mark.parametrize("label", ["ANALYTICS_QUERY", "METRIC_DEFINITION", "UNSUPPORTED"])
def test_parse_each_valid_label(label):
    result = parse_intent_output({"intent": label})
    assert result.intent == label


def test_parse_invalid_label_raises():
    with pytest.raises(ValidationError):
        parse_intent_output({"intent": "SOMETHING_ELSE"})


def test_parse_missing_field_raises():
    with pytest.raises(ValidationError):
        parse_intent_output({})
