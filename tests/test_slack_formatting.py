from service import AnswerOutcome
from slack.formatting import format_outcome_blocks, strip_mention


def test_strip_mention_removes_leading_mention():
    assert strip_mention("<@U123ABC> revenue by region") == "revenue by region"


def test_strip_mention_no_mention_present():
    assert strip_mention("revenue by region") == "revenue by region"


def test_strip_mention_only_mention():
    assert strip_mention("<@U123ABC>") == ""


def test_format_outcome_blocks_basic_shape():
    outcome = AnswerOutcome(intent="ANALYTICS_QUERY", text="Revenue is highest in the East region.")
    blocks = format_outcome_blocks("revenue by region", outcome)
    assert blocks[0]["text"]["text"] == "*Q:* revenue by region"
    assert blocks[1]["text"]["text"] == "Revenue is highest in the East region."
    assert len(blocks) == 2  # no repair-log context block when repair_log is empty


def test_format_outcome_blocks_includes_repair_log_context():
    outcome = AnswerOutcome(
        intent="ANALYTICS_QUERY",
        text="Revenue is highest in the East region.",
        repair_log=["structural repair attempt 1: ...", "schema-resolution repair attempt 1: ..."],
    )
    blocks = format_outcome_blocks("revenue by region", outcome)
    assert len(blocks) == 3
    assert blocks[2]["type"] == "context"
    assert "structural repair attempt 1" in blocks[2]["elements"][0]["text"]
    assert "schema-resolution repair attempt 1" in blocks[2]["elements"][0]["text"]


def test_format_outcome_blocks_unsupported_intent():
    outcome = AnswerOutcome(intent="UNSUPPORTED", text="This agent only answers analytics questions.")
    blocks = format_outcome_blocks("what's the weather", outcome)
    assert "This agent only answers" in blocks[1]["text"]["text"]
