#!/usr/bin/env python
"""Eval harness: runs eval/benchmark.jsonl through the real pipeline and
reports the metrics from ARCHITECTURE.md's Evaluation Harness section.

Requires ANTHROPIC_API_KEY (every non-`unsupported_unsafe` case drives a
live LLM call). `execution_accuracy` additionally requires `DATABASE_URL`
pointing at a live, seeded Postgres (`make setup`) -- reported as N/A
otherwise, with every other metric still computed.

Not covered by the test suite itself (it IS the thing that needs live
credentials); what it calls is: scoring.py (unit-tested,
tests/test_scoring.py), the benchmark data (validated,
tests/test_benchmark_integrity.py), and planner/repair.py's run_pipeline
(unit-tested with fake LLM callables, tests/test_repair.py).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from compiler.sql_compiler import compile_sql  # noqa: E402
from joinplanner.sql_builder import SchemaCatalog, plan_join_for_fields  # noqa: E402
from loader import BenchmarkCase, load_benchmark  # noqa: E402
from planner import date_resolver  # noqa: E402
from planner.glossary import load_glossary  # noqa: E402
from planner.query_plan import AbsoluteTimeRange, QueryPlan  # noqa: E402
from planner.repair import run_pipeline  # noqa: E402
from resolver.schema_resolver import apply_field_rewrites, resolve_query_plan_fields  # noqa: E402
from router.intent_router import classify_intent  # noqa: E402
from safety.guard import check_read_only  # noqa: E402
from scoring import correct_join_path, dimension_accuracy, filter_accuracy, metric_accuracy, rows_match, time_range_accuracy  # noqa: E402
from service import make_llm_functions  # noqa: E402

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


@dataclass
class CaseResult:
    case_id: str
    category: str
    intent_correct: bool
    executable: bool | None = None
    metric_acc: float | None = None
    dimension_acc: float | None = None
    filter_acc: float | None = None
    time_range_acc: float | None = None
    join_correct: bool | None = None
    execution_acc: bool | None = None
    repaired: bool = False
    repair_ok: bool | None = None


def _compile_expected_sql(plan: QueryPlan, schema: SchemaCatalog) -> str | None:
    """Compile the ground-truth plan through the SAME deterministic pipeline
    the actual answer goes through, so execution_accuracy compares RESULT
    ROWS from two independently-specified plans, not two SQL texts. Returns
    None if the expected plan itself can't be resolved/joined (should not
    happen given test_benchmark_integrity.py, but fail soft here rather than
    crash a whole eval run over one bad case).
    """
    resolution = resolve_query_plan_fields(plan, schema)
    if not resolution.ok:
        return None
    plan = apply_field_rewrites(plan, resolution.field_rewrites)
    if plan.time_range is not None and not isinstance(plan.time_range, AbsoluteTimeRange):
        plan = plan.model_copy(update={"time_range": date_resolver.resolve(plan.time_range, __import__("datetime").date.today())})
    try:
        join_plan, field_table = plan_join_for_fields(schema, resolution.schema_fields)
    except ValueError:
        return None
    sql = compile_sql(plan, join_plan, field_table)
    guard_result = check_read_only(sql)
    return guard_result.sql if guard_result.ok else None


def run_case(case: BenchmarkCase, schema: SchemaCatalog, glossary_text: str) -> CaseResult:
    classification = classify_intent(case.question)

    if case.category == "unsupported_unsafe":
        return CaseResult(
            case_id=case.id, category=case.category,
            intent_correct=(classification.intent == "UNSUPPORTED"),
        )

    intent_correct = classification.intent == "ANALYTICS_QUERY"

    planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn = make_llm_functions(
        schema, glossary_text
    )
    outcome = run_pipeline(
        case.question, schema, planner_fn, structural_repair_fn, schema_resolution_repair_fn, semantic_replan_fn
    )

    result = CaseResult(
        case_id=case.id, category=case.category, intent_correct=intent_correct,
        executable=outcome.ok, repaired=bool(outcome.repair_log),
    )
    if result.repaired:
        result.repair_ok = outcome.ok

    if not outcome.ok or outcome.query_plan is None:
        return result

    actual = outcome.query_plan
    expected = case.expected_plan
    result.metric_acc = metric_accuracy(actual, expected)
    result.dimension_acc = dimension_accuracy(actual, expected)
    result.filter_acc = filter_accuracy(actual, expected)
    result.time_range_acc = time_range_accuracy(actual, expected)
    result.join_correct = correct_join_path(actual, expected, schema)

    if "DATABASE_URL" in os.environ:
        from db.postgres import execute

        expected_sql = _compile_expected_sql(expected, schema)
        if expected_sql is not None:
            try:
                actual_result = execute(outcome.sql)
                expected_result = execute(expected_sql)
                result.execution_acc = rows_match(actual_result.rows, expected_result.rows)
            except Exception:  # noqa: BLE001 -- a DB failure mid-eval shouldn't crash the whole run
                result.execution_acc = None

    return result


def summarize(results: list[CaseResult]) -> None:
    def avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def fmt(v: float | None) -> str:
        return "N/A" if v is None else f"{v:.1%}"

    non_unsafe = [r for r in results if r.category != "unsupported_unsafe"]
    unsafe = [r for r in results if r.category == "unsupported_unsafe"]
    executable_cases = [r for r in non_unsafe if r.executable]
    repaired_cases = [r for r in results if r.repaired]

    print(f"\n{'='*60}\nOverall ({len(results)} cases)\n{'='*60}")
    print(f"executable_sql_rate:       {fmt(avg([1.0 if r.executable else 0.0 for r in non_unsafe]))}")
    print(f"correct_join_path_rate:    {fmt(avg([1.0 if r.join_correct else 0.0 for r in executable_cases]))}")
    print(f"metric_accuracy:           {fmt(avg([r.metric_acc for r in executable_cases if r.metric_acc is not None]))}")
    print(f"dimension_accuracy:        {fmt(avg([r.dimension_acc for r in executable_cases if r.dimension_acc is not None]))}")
    print(f"filter_accuracy:           {fmt(avg([r.filter_acc for r in executable_cases if r.filter_acc is not None]))}")
    print(f"time_range_accuracy:       {fmt(avg([r.time_range_acc for r in executable_cases if r.time_range_acc is not None]))}")
    exec_acc_values = [1.0 if r.execution_acc else 0.0 for r in executable_cases if r.execution_acc is not None]
    print(f"execution_accuracy:        {fmt(avg(exec_acc_values)) if exec_acc_values else 'N/A (DATABASE_URL not set)'}")
    print(f"unsafe_query_blocking_rate: {fmt(avg([1.0 if r.intent_correct else 0.0 for r in unsafe]))}")
    if repaired_cases:
        print(f"repair_success_rate:       {fmt(avg([1.0 if r.repair_ok else 0.0 for r in repaired_cases]))} "
              f"({len(repaired_cases)} of {len(results)} cases needed repair)")
    else:
        print("repair_success_rate:       N/A (no case needed repair)")

    print(f"\n{'='*60}\nBy category\n{'='*60}")
    categories = sorted({r.category for r in results})
    for cat in categories:
        cat_results = [r for r in results if r.category == cat]
        if cat == "unsupported_unsafe":
            rate = avg([1.0 if r.intent_correct else 0.0 for r in cat_results])
            print(f"{cat:24s} blocking_rate={fmt(rate)}  ({len(cat_results)} cases)")
        else:
            exec_rate = avg([1.0 if r.executable else 0.0 for r in cat_results])
            print(f"{cat:24s} executable_rate={fmt(exec_rate)}  ({len(cat_results)} cases)")


def main() -> None:
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ANTHROPIC_API_KEY is not set -- the eval harness needs it for every case.", file=sys.stderr)
        raise SystemExit(1)

    schema = SchemaCatalog(CORRELATION_PATH)
    glossary_text = load_glossary()
    cases = load_benchmark()

    if "DATABASE_URL" not in os.environ:
        print("DATABASE_URL not set -- execution_accuracy will be reported as N/A.", file=sys.stderr)

    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case.id}: {case.question}", file=sys.stderr)
        results.append(run_case(case, schema, glossary_text))

    summarize(results)


if __name__ == "__main__":
    main()
