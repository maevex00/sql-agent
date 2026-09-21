"""LLM Query Planner: natural language -> QueryPlan.

This is the LLM's entire footprint in the pipeline's "understand intent"
half: one forced tool-use call against QueryPlan's own JSON schema. It never
sees table names or SQL -- only the flat field catalog
(SchemaCatalog.all_fields()) and the user's question.

`parse_llm_output` is a pure function (no network call), so the
parsing/validation path -- exactly what the Structural repair type in
planner/repair.py operates on -- is unit-testable without an API key.
`plan_query` wraps it with the actual Anthropic call and is not exercised by
the test suite (see tests/test_llm_planner.py, which tests parse_llm_output
only).
"""
from __future__ import annotations

import os
from typing import Any

from planner.query_plan import QueryPlan

SYSTEM_PROMPT = """You are the query planner for an enterprise analytics agent.

Given a business question and a catalog of available field names, call
submit_query_plan with a structured plan: which fields to aggregate (metrics),
which to group by (dimensions), any filters, an optional time range, any
post-aggregation conditions (having / metric_comparisons), sort order, and a
row limit.

Rules:
- Only ever reference field names from the provided catalog. Never invent one.
- Never produce SQL, table names, or JOIN logic -- that is handled elsewhere.
- Prefer a relative time range (e.g. period="last_quarter") over guessing
  absolute dates; only use an absolute range if the user gave explicit dates.
- For "metric-vs-metric" questions ("which regions missed their targets?"),
  use metric_comparisons, not a filter.
"""


def build_tool_schema() -> dict[str, Any]:
    return {
        "name": "submit_query_plan",
        "description": "Submit the structured query plan for the user's question.",
        "input_schema": QueryPlan.model_json_schema(),
    }


def build_system_prompt(glossary_text: str = "") -> str:
    """SYSTEM_PROMPT, optionally extended with the Glossary Layer's content
    (planner/glossary.py). Kept as a separate function rather than baked
    into `plan_query` so it's easy to inspect/test what the LLM actually sees.
    """
    if not glossary_text:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\nBusiness terminology and standard metric definitions:\n\n{glossary_text}"


def parse_llm_output(raw_tool_input: dict[str, Any]) -> QueryPlan:
    """Pure function: raw tool-call JSON -> validated QueryPlan.

    Raises pydantic.ValidationError on structural problems -- caught by
    planner/repair.py's STRUCTURAL repair path, nowhere else.
    """
    return QueryPlan.model_validate(raw_tool_input)


def plan_query(
    question: str,
    field_catalog: list[str],
    *,
    glossary_text: str = "",
    model: str = "claude-sonnet-5",
    api_key: str | None = None,
) -> QueryPlan:
    """NL question -> QueryPlan via a single forced tool-use call.

    Requires the `anthropic` package and an API key (arg or
    ANTHROPIC_API_KEY env var). Not covered by unit tests -- see module
    docstring.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    catalog_text = ", ".join(sorted(field_catalog))
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=build_system_prompt(glossary_text),
        tools=[build_tool_schema()],
        tool_choice={"type": "tool", "name": "submit_query_plan"},
        messages=[
            {"role": "user", "content": f"Available fields: {catalog_text}\n\nQuestion: {question}"}
        ],
    )
    tool_use = next(block for block in response.content if block.type == "tool_use")
    return parse_llm_output(tool_use.input)
