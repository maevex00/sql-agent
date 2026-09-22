"""Pure scoring functions for the eval harness -- no network, no LLM, no DB.

Ground truth for each benchmark case is itself a `QueryPlan` (see
generate_benchmark.py), so comparisons are QueryPlan-vs-QueryPlan, not
QueryPlan-vs-hand-typed-dict. This is what makes every function here
unit-testable with two hand-built QueryPlan objects (tests/test_scoring.py).

Per-component scores (metric/dimension/filter) use Jaccard similarity
(intersection over union): these are legitimately partial-credit-able --
asking for "region and category" when only "region" was expected is a
partial match, not simply wrong. `correct_join_path` uses exact set equality
instead: a join that's missing one required table isn't "mostly right," the
query is just wrong, so partial credit would be misleading there.
"""
from __future__ import annotations

from datetime import date

from joinplanner.sql_builder import SchemaCatalog, plan_join_for_fields
from planner import date_resolver
from planner.query_plan import QueryPlan
from resolver.schema_resolver import resolve_query_plan_fields


def _jaccard(actual: set, expected: set) -> float:
    if not actual and not expected:
        return 1.0
    if not expected:
        return 0.0
    return len(actual & expected) / len(actual | expected)


def metric_accuracy(actual: QueryPlan, expected: QueryPlan) -> float:
    return _jaccard(
        {(m.field, m.func) for m in actual.metrics},
        {(m.field, m.func) for m in expected.metrics},
    )


def dimension_accuracy(actual: QueryPlan, expected: QueryPlan) -> float:
    return _jaccard(set(actual.dimensions), set(expected.dimensions))


def filter_accuracy(actual: QueryPlan, expected: QueryPlan) -> float:
    return _jaccard(
        {(f.field, f.op, str(f.value)) for f in actual.filters},
        {(f.field, f.op, str(f.value)) for f in expected.filters},
    )


def time_range_accuracy(actual: QueryPlan, expected: QueryPlan, today: date | None = None) -> float:
    """Compares time ranges by what they RESOLVE to, not by `kind`.

    `planner.repair.run_pipeline` always resolves a relative time range to
    absolute dates before returning the plan (that resolution is the whole
    point of date_resolver -- see ARCHITECTURE.md's QueryPlan section), so a
    live pipeline's `actual.time_range` is essentially always
    `AbsoluteTimeRange`, even when the LLM picked the exactly-correct
    relative period. Comparing `kind` directly against a benchmark's
    still-relative `expected.time_range` would then fail every single
    time-series case regardless of correctness -- caught against the live
    benchmark run, not a fake callable, since fakes never modeled this
    resolution step. `today` defaults to the real clock but is an explicit
    parameter (never read internally) so this stays deterministically
    testable, exactly like date_resolver.resolve() itself.
    """
    if expected.time_range is None:
        return 1.0 if actual.time_range is None else 0.0
    if actual.time_range is None:
        return 0.0

    today = today or date.today()

    def _as_absolute(tr):
        return date_resolver.resolve(tr, today) if tr.kind == "relative" else tr

    exp_abs = _as_absolute(expected.time_range)
    act_abs = _as_absolute(actual.time_range)
    return 1.0 if (act_abs.field, act_abs.start, act_abs.end) == (exp_abs.field, exp_abs.start, exp_abs.end) else 0.0


def resolved_tables(plan: QueryPlan, schema: SchemaCatalog) -> frozenset[str] | None:
    """The set of tables plan's fields resolve to via the real join planner,
    or None if any field is unresolvable or the fields can't be joined at
    all. Used both to score `correct_join_path` and, at benchmark-generation
    time, to self-verify that every ground-truth case is actually achievable
    against the real schema (see tests/test_benchmark_integrity.py).
    """
    fields = list(plan.dimensions)
    fields += [m.field for m in plan.metrics]
    fields += [f.field for f in plan.filters]
    if plan.time_range is not None:
        fields.append(plan.time_range.field)
    for mc in plan.metric_comparisons:
        fields.append(mc.left.field)
        fields.append(mc.right.field)
    if not fields:
        return frozenset()

    resolution = resolve_query_plan_fields(plan, schema)
    if not resolution.ok:
        return None
    try:
        join_plan, _ = plan_join_for_fields(schema, resolution.schema_fields)
    except ValueError:
        return None
    return frozenset(step.table for step in join_plan.steps)


def correct_join_path(actual: QueryPlan, expected: QueryPlan, schema: SchemaCatalog) -> bool:
    actual_tables = resolved_tables(actual, schema)
    expected_tables = resolved_tables(expected, schema)
    if actual_tables is None or expected_tables is None:
        return False
    return actual_tables == expected_tables


def rows_match(actual_rows: list[tuple], expected_rows: list[tuple]) -> bool:
    """Result-set comparison for execution_accuracy: order-independent, since
    neither query is guaranteed a stable row order without an explicit sort
    both plans agree on. Requires DATABASE_URL to obtain either side's rows
    in the first place -- see eval/run_eval.py; this function itself needs
    no database and is unit-tested directly with hand-built row lists.
    """
    return sorted(map(tuple, actual_rows)) == sorted(map(tuple, expected_rows))
