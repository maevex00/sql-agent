"""Intent Router: the pipeline's first and thinnest stage.

Deliberately three categories only (ARCHITECTURE.md, "Intent Router
simplified") -- the point of this project is semantic query planning and
schema reasoning, not intent classification, so this stage stays a single
cheap LLM call with a closed label set, not a routing system of its own.

  ANALYTICS_QUERY     -> the full Phase 5 pipeline (LLM Query Planner ->
                          Schema Resolver -> Repair Loop -> ... -> SQL)
  METRIC_DEFINITION    -> answered directly from the Glossary Layer, no SQL
  UNSUPPORTED          -> declined with a fixed message, no further LLM calls

`parse_intent_output` is pure (no network call) and is what the test suite
covers; `classify_intent` wraps the real Anthropic call, same pattern as
planner/llm_planner.py.
"""
from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel

IntentLabel = Literal["ANALYTICS_QUERY", "METRIC_DEFINITION", "UNSUPPORTED"]

SYSTEM_PROMPT = """Classify the user's question into exactly one of three categories:

- ANALYTICS_QUERY: a question answerable by querying the analytics database (aggregations,
  trends, comparisons, rankings, filtered lookups over the business data).
- METRIC_DEFINITION: a question about what a term or metric MEANS ("what counts as a
  completed order?", "how is revenue defined here?"), not a request to compute a number.
- UNSUPPORTED: anything else -- chit-chat, requests outside the analytics domain, or
  requests to modify data (this agent is read-only).

Call classify_intent with your answer."""


class IntentClassification(BaseModel):
    intent: IntentLabel


def build_tool_schema() -> dict[str, Any]:
    return {
        "name": "classify_intent",
        "description": "Submit the classified intent for the user's question.",
        "input_schema": IntentClassification.model_json_schema(),
    }


def parse_intent_output(raw_tool_input: dict[str, Any]) -> IntentClassification:
    """Pure function: raw tool-call JSON -> validated IntentClassification.

    Raises pydantic.ValidationError on structural problems. The Intent Router
    has no repair loop of its own (ARCHITECTURE.md keeps this stage thin) --
    a malformed classification is treated as UNSUPPORTED by the caller
    (scripts/ask.py), not retried.
    """
    return IntentClassification.model_validate(raw_tool_input)


def classify_intent(
    question: str,
    *,
    model: str = "claude-sonnet-5",
    api_key: str | None = None,
) -> IntentClassification:
    """Question -> IntentClassification via a single forced tool-use call.

    Requires the `anthropic` package and an API key. Not covered by unit
    tests -- see module docstring.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        tools=[build_tool_schema()],
        tool_choice={"type": "tool", "name": "classify_intent"},
        messages=[{"role": "user", "content": question}],
    )
    tool_use = next(block for block in response.content if block.type == "tool_use")
    return parse_intent_output(tool_use.input)
