#!/usr/bin/env python
"""CLI: ask a natural-language analytics question, get an answer.

This is the Phase 5-7 milestone: Intent Router -> Glossary Layer -> LLM Query
Planner -> Schema Resolver -> Repair Loop -> JOIN Planner -> Compiler ->
Safety Layer -> (if DATABASE_URL is reachable) Postgres execution -> Result
Analyzer, running end-to-end. No Slack integration yet.

If DATABASE_URL is not set or the connection fails, this falls back to
printing the compiled SQL without executing it -- the deterministic pipeline
up to and including the safety layer is still fully exercised either way.

Requires ANTHROPIC_API_KEY. Not covered by the test suite (which tests each
stage's orchestration/parsing with fake functions or hand-built inputs --
see tests/test_repair.py, tests/test_intent_router.py, tests/test_analyzer.py,
and friends); this script is the thin, unavoidably-untestable-without-
credentials glue around real LLM and DB calls.
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
from planner.glossary import answer_metric_definition, load_glossary  # noqa: E402
from planner.query_plan import QueryPlan  # noqa: E402
from planner.repair import run_pipeline  # noqa: E402
from report.analyzer import analyze  # noqa: E402
from resolver.schema_resolver import FieldResolution  # noqa: E402
from router.intent_router import classify_intent  # noqa: E402

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


def make_llm_functions(schema: SchemaCatalog, glossary_text: str):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    catalog_text = ", ".join(sorted(schema.all_fields()))
    system_prompt = llm_planner.build_system_prompt(glossary_text)

    def planner_fn(question: str) -> dict:
        return _call_tool(
            client,
            system_prompt,
            f"Available fields: {catalog_text}\n\nQuestion: {question}",
        )

    def structural_repair_fn(question: str, bad_raw: dict, error_msg: str) -> dict:
        return _call_tool(
            client,
            system_prompt,
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
            system_prompt,
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
    glossary_text = load_glossary()

    classification = classify_intent(args.question)
    print(f"--- intent: {classification.intent} ---", file=sys.stderr)

    if classification.intent == "UNSUPPORTED":
        print("This agent only answers analytics questions and metric definitions over the")
        print("demo dataset -- it can't help with that, and it's read-only regardless.")
        raise SystemExit(1)

    if classification.intent == "METRIC_DEFINITION":
        print(answer_metric_definition(args.question, glossary_text))
        return

    planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn = make_llm_functions(
        schema, glossary_text
    )

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

    if "DATABASE_URL" not in os.environ:
        print("--- DATABASE_URL not set, printing SQL without executing it ---", file=sys.stderr)
        print(outcome.sql)
        return

    from db.postgres import execute

    try:
        result = execute(outcome.sql)
    except Exception as e:  # noqa: BLE001 -- surfacing any connection/execution failure, not handling it
        print(f"--- could not execute against Postgres ({e}); printing SQL instead ---", file=sys.stderr)
        print(outcome.sql)
        return

    analysis = analyze(args.question, outcome.query_plan, result.columns, result.rows)
    print(analysis.summary)
    if analysis.chart_path:
        print(f"(chart saved to {analysis.chart_path})", file=sys.stderr)


if __name__ == "__main__":
    main()
