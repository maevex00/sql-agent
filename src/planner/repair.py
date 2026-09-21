"""Repair Loop orchestration: end-to-end NL question -> guarded SQL.

Three distinct repair mechanisms, matching ARCHITECTURE.md's Repair Loop
section -- they are NOT funneled through one generic "ask the LLM to fix the
JSON" call:

  STRUCTURAL         raw LLM tool output fails Pydantic validation -> feed
                      the validation error back, ask only for corrected JSON.
  SCHEMA_RESOLUTION   a field reference doesn't resolve against the schema
                      catalog -> resolver proposes a constrained candidate
                      list; the repair function may only pick from it.
  SEMANTIC_REPLAN     plan is structurally and referentially valid but the
                      JOIN planner can't connect the required tables at all
                      -> full re-prompt with the original question + why the
                      prior plan failed.

Each type gets its own attempt budget (MAX_ATTEMPTS_PER_TYPE), plus a shared
ceiling (MAX_TOTAL_REPAIR_CYCLES) bounding worst-case latency across all
three. The LLM-calling functions are injected as callables so this
orchestration is testable with deterministic fakes -- no network, no API
key -- see tests/test_repair.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from pydantic import ValidationError

from compiler.sql_compiler import compile_sql
from joinplanner.sql_builder import SchemaCatalog, plan_join_for_fields
from planner import date_resolver
from planner.llm_planner import parse_llm_output
from planner.query_plan import QueryPlan
from resolver.schema_resolver import FieldResolution, apply_field_rewrites, resolve_query_plan_fields
from safety.guard import check_read_only

MAX_ATTEMPTS_PER_TYPE = 2
MAX_TOTAL_REPAIR_CYCLES = 3

PlannerFn = Callable[[str], dict[str, Any]]
StructuralRepairFn = Callable[[str, dict[str, Any], str], dict[str, Any]]
SchemaResolutionRepairFn = Callable[[str, list[FieldResolution]], dict[str, str]]
SemanticReplanFn = Callable[[str, QueryPlan, str], dict[str, Any]]


@dataclass
class PipelineOutcome:
    ok: bool
    query_plan: QueryPlan | None = None
    sql: str | None = None
    error: str | None = None
    repair_log: list[str] = field(default_factory=list)


def run_pipeline(
    question: str,
    schema: SchemaCatalog,
    planner_fn: PlannerFn,
    structural_repair_fn: StructuralRepairFn,
    schema_resolution_repair_fn: SchemaResolutionRepairFn,
    semantic_replan_fn: SemanticReplanFn,
    *,
    today: date | None = None,
    max_limit: int = 10_000,
) -> PipelineOutcome:
    log: list[str] = []
    total_repairs = 0
    today = today or date.today()

    raw = planner_fn(question)

    # --- Structural: validate the raw LLM output against QueryPlan's schema ---
    plan: QueryPlan | None = None
    structural_attempts = 0
    while plan is None:
        try:
            plan = parse_llm_output(raw)
        except ValidationError as e:
            structural_attempts += 1
            total_repairs += 1
            if structural_attempts > MAX_ATTEMPTS_PER_TYPE or total_repairs > MAX_TOTAL_REPAIR_CYCLES:
                return PipelineOutcome(ok=False, error=f"structural repair exhausted: {e}", repair_log=log)
            log.append(f"structural repair attempt {structural_attempts}: {e}")
            raw = structural_repair_fn(question, raw, str(e))

    # --- Schema-resolution: every field reference must resolve to a real column ---
    schema_attempts = 0
    resolution = resolve_query_plan_fields(plan, schema)
    while not resolution.ok:
        schema_attempts += 1
        total_repairs += 1
        if schema_attempts > MAX_ATTEMPTS_PER_TYPE or total_repairs > MAX_TOTAL_REPAIR_CYCLES:
            unresolved = ", ".join(u.original for u in resolution.unresolved)
            return PipelineOutcome(
                ok=False, error=f"schema-resolution repair exhausted for: {unresolved}", repair_log=log
            )
        log.append(
            f"schema-resolution repair attempt {schema_attempts}: "
            f"{[(u.original, u.candidates) for u in resolution.unresolved]}"
        )
        chosen = schema_resolution_repair_fn(question, resolution.unresolved)
        plan = apply_field_rewrites(plan, chosen)
        resolution = resolve_query_plan_fields(plan, schema)

    plan = apply_field_rewrites(plan, resolution.field_rewrites)  # apply auto (non-error) corrections too

    # --- Relative time is never resolved by the LLM; do it here, deterministically ---
    if plan.time_range is not None:
        absolute = date_resolver.resolve(plan.time_range, today)
        plan = plan.model_copy(update={"time_range": absolute})

    # --- Semantic replan: JOIN planner can't connect the required tables at all ---
    replan_attempts = 0
    while True:
        try:
            join_plan, field_table = plan_join_for_fields(schema, resolution.schema_fields)
            break
        except ValueError as e:
            replan_attempts += 1
            total_repairs += 1
            if replan_attempts > MAX_ATTEMPTS_PER_TYPE or total_repairs > MAX_TOTAL_REPAIR_CYCLES:
                return PipelineOutcome(ok=False, error=f"semantic replan exhausted: {e}", repair_log=log)
            log.append(f"semantic replan attempt {replan_attempts}: {e}")
            raw = semantic_replan_fn(question, plan, str(e))
            try:
                plan = parse_llm_output(raw)
            except ValidationError as e2:
                return PipelineOutcome(
                    ok=False, error=f"semantic replan produced structurally invalid output: {e2}", repair_log=log
                )
            resolution = resolve_query_plan_fields(plan, schema)
            if not resolution.ok:
                return PipelineOutcome(
                    ok=False, error="semantic replan produced unresolved field references", repair_log=log
                )
            plan = apply_field_rewrites(plan, resolution.field_rewrites)

    sql = compile_sql(plan, join_plan, field_table, max_limit=max_limit)
    guard_result = check_read_only(sql, max_limit=max_limit)
    if not guard_result.ok:
        # Not a repair case: the compiler is trusted to only emit safe SQL, so a
        # rejection here is an internal bug, surfaced as an error, not retried.
        return PipelineOutcome(
            ok=False, error=f"safety layer rejected compiled SQL: {guard_result.reason}", repair_log=log
        )

    return PipelineOutcome(ok=True, query_plan=plan, sql=guard_result.sql, repair_log=log)
