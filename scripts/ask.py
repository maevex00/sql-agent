#!/usr/bin/env python
"""CLI: ask a natural-language analytics question, get compiled + guarded SQL.

This is the Phase 5 milestone: the full pipeline (LLM Query Planner -> Schema
Resolver -> Repair Loop -> JOIN Planner -> Compiler -> Safety Layer) running
end-to-end, with no Slack integration yet. It prints SQL; it does not execute
it against a database -- that needs `docker compose up` (Postgres) and the
DB execution layer, which are later-phase work per ARCHITECTURE.md.

Requires ANTHROPIC_API_KEY. Not covered by the test suite (which tests
run_pipeline's orchestration with fake planner/repair functions, and every
deterministic stage independently -- see tests/test_repair.py and friends);
this script is the thin, unavoidably-untestable-without-a-key glue around
real LLM calls.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from joinplanner.sql_builder import SchemaCatalog  # noqa: E402
from planner import llm_planner  # noqa: E402
from planner.query_plan import QueryPlan  # noqa: E402
from planner.repair import run_pipeline  # noqa: E402
from resolver.schema_resolver import FieldResolution  # noqa: E402

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


def _call_tool(client, system_prompt: str, user_content: str, model: str = "claude-sonnet-5") -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        tools=[llm_planner.build_tool_schema()],
        tool_choice={"type": "tool", "name": "submit_query_plan"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input


def make_llm_functions(schema: SchemaCatalog):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    catalog_text = ", ".join(sorted(schema.all_fields()))

    def planner_fn(question: str) -> dict:
        return _call_tool(
            client,
            llm_planner.SYSTEM_PROMPT,
            f"Available fields: {catalog_text}\n\nQuestion: {question}",
        )

    def structural_repair_fn(question: str, bad_raw: dict, error_msg: str) -> dict:
        return _call_tool(
            client,
            llm_planner.SYSTEM_PROMPT,
            f"Your previous submit_query_plan call was invalid.\n"
            f"Error: {error_msg}\nPrevious output: {json.dumps(bad_raw)}\n"
            f"Available fields: {catalog_text}\nQuestion: {question}\n"
            f"Call submit_query_plan again with corrected JSON only.",
        )

    def schema_resolution_repair_fn(question: str, unresolved: list[FieldResolution]) -> dict:
        lines = [f"'{u.original}' -> candidates: {u.candidates}" for u in unresolved]
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=512,
            system=(
                "You resolve ambiguous field references. Reply with strict JSON only: "
                '{"<original>": "<chosen_candidate>", ...}. You may ONLY choose from the '
                "given candidates for each field -- never invent a new name."
            ),
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def semantic_replan_fn(question: str, prior_plan: QueryPlan, reason: str) -> dict:
        return _call_tool(
            client,
            llm_planner.SYSTEM_PROMPT,
            f"Your previous query plan could not be executed: {reason}\n"
            f"Previous plan: {prior_plan.model_dump_json()}\n"
            f"Available fields: {catalog_text}\nQuestion: {question}\n"
            f"Call submit_query_plan again with a plan that avoids this problem "
            f"(e.g. drop or replace fields that cannot be joined together).",
        )

    return planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask a natural-language analytics question.")
    parser.add_argument("question")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ANTHROPIC_API_KEY is not set -- the LLM-backed pipeline needs it.", file=sys.stderr)
        print(
            "The deterministic core (join planning, compiler, safety layer, and the "
            "repair-loop orchestration itself) has its own test suite that runs without "
            "an API key: `pytest -v`.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    schema = SchemaCatalog(CORRELATION_PATH)
    planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn = make_llm_functions(schema)

    outcome = run_pipeline(
        args.question, schema, planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn
    )

    if outcome.repair_log:
        print("--- repair log ---", file=sys.stderr)
        for line in outcome.repair_log:
            print(f"  {line}", file=sys.stderr)

    if not outcome.ok:
        print(f"FAILED: {outcome.error}", file=sys.stderr)
        raise SystemExit(1)

    print(outcome.sql)


if __name__ == "__main__":
    main()
