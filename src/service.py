"""Application service: the single orchestration point both scripts/ask.py
(CLI) and slack/app.py (Phase 8) consume.

Ties together Intent Router -> Glossary Layer -> LLM Query Planner -> Repair
Loop -> (Postgres execution + Result Analyzer, when DATABASE_URL is
reachable) into one function, so the two front-ends don't each re-implement
this wiring -- they only differ in how they present an AnswerOutcome to the
user. This module itself is not covered by unit tests (every call it makes
is either a live LLM call or a live DB call); the orchestration logic it
wraps is tested independently with fake callables -- see
planner/repair.py + tests/test_repair.py for the part that matters.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from joinplanner.sql_builder import SchemaCatalog
from planner import llm_planner
from planner.glossary import answer_metric_definition
from planner.query_plan import QueryPlan
from planner.repair import run_pipeline
from report.analyzer import analyze
from resolver.schema_resolver import FieldResolution
from router.intent_router import IntentLabel, classify_intent


@dataclass
class AnswerOutcome:
    intent: IntentLabel
    text: str
    sql: str | None = None
    chart_path: str | None = None
    repair_log: list[str] = field(default_factory=list)
    error: str | None = None


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
        return _call_tool(client, system_prompt, f"Available fields: {catalog_text}\n\nQuestion: {question}")

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


def answer_question(question: str, schema: SchemaCatalog, glossary_text: str) -> AnswerOutcome:
    """Requires ANTHROPIC_API_KEY. DATABASE_URL is optional -- falls back to
    returning the compiled SQL as text when unset or unreachable, same
    fallback scripts/ask.py had before this module existed.
    """
    classification = classify_intent(question)

    if classification.intent == "UNSUPPORTED":
        return AnswerOutcome(
            intent="UNSUPPORTED",
            text=(
                "This agent only answers analytics questions and metric definitions over the "
                "demo dataset -- it can't help with that, and it's read-only regardless."
            ),
        )

    if classification.intent == "METRIC_DEFINITION":
        return AnswerOutcome(intent="METRIC_DEFINITION", text=answer_metric_definition(question, glossary_text))

    planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn = make_llm_functions(
        schema, glossary_text
    )
    outcome = run_pipeline(
        question, schema, planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn
    )

    if not outcome.ok:
        return AnswerOutcome(
            intent="ANALYTICS_QUERY",
            text=f"Couldn't answer that: {outcome.error}",
            repair_log=outcome.repair_log,
            error=outcome.error,
        )

    if "DATABASE_URL" not in os.environ:
        return AnswerOutcome(
            intent="ANALYTICS_QUERY",
            text=f"```{outcome.sql}```\n(DATABASE_URL not set -- showing compiled SQL, not executed.)",
            sql=outcome.sql,
            repair_log=outcome.repair_log,
        )

    from db.postgres import execute

    try:
        result = execute(outcome.sql)
    except Exception as e:  # noqa: BLE001 -- surfacing any connection/execution failure, not handling it
        return AnswerOutcome(
            intent="ANALYTICS_QUERY",
            text=f"```{outcome.sql}```\n(Could not execute against Postgres: {e})",
            sql=outcome.sql,
            repair_log=outcome.repair_log,
        )

    analysis = analyze(question, outcome.query_plan, result.columns, result.rows)
    return AnswerOutcome(
        intent="ANALYTICS_QUERY",
        text=analysis.summary,
        sql=outcome.sql,
        chart_path=analysis.chart_path,
        repair_log=outcome.repair_log,
    )
