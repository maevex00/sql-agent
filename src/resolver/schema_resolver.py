"""Schema / Field Resolver: semantic field references in a QueryPlan -> real
schema field names.

The LLM is not trusted to know the schema catalog's exact spelling -- it is
given the field list in its prompt and instructed to use it, but "synonym
matching" here means catching near-misses (case, punctuation, minor
misspellings), not business-vocabulary translation. Domain vocabulary (e.g.
"revenue" -> the right metric) is the Glossary Layer's job, injected into the
LLM's prompt upstream -- see ARCHITECTURE.md pipeline stage 2 vs stage 4.

A reference that resolves to zero or multiple equally-plausible candidates is
reported, not silently guessed -- that is exactly the Schema-resolution
repair path's input (planner/repair.py).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field as _field

from joinplanner.sql_builder import SchemaCatalog
from planner.query_plan import QueryPlan

FUZZY_MATCH_CUTOFF = 0.72
# How much better the best fuzzy candidate must score than the runner-up to be
# treated as a confident single match, rather than genuine ambiguity. A schema
# with many `*_name` columns (region_name, product_name, promotion_name, ...)
# makes plain cutoff-based matching too coarse -- e.g. "prodct_name" scores
# above cutoff against BOTH product_name and promotion_name, but only one of
# those is actually a close call once you compare their scores to each other.
FUZZY_MARGIN = 0.08


@dataclass
class FieldResolution:
    original: str
    resolved: str | None  # None if unresolved
    candidates: list[str]  # non-empty when resolved via fuzzy match, or when unresolved
    context: str = ""  # e.g. "metrics[0].field", filled in by resolve_query_plan_fields


def _normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def resolve_field_name(raw: str, schema: SchemaCatalog) -> FieldResolution:
    """Exact match -> normalized match -> margin-based fuzzy match -> unresolved.

    Fuzzy matching ranks every known field by similarity and only accepts the
    top one if it clears both an absolute floor (FUZZY_MATCH_CUTOFF) and a
    relative margin over the runner-up (FUZZY_MARGIN). Two candidates that
    are both plausible and close to each other in score are reported as
    genuine ambiguity (resolved=None, both in candidates) rather than the
    resolver picking one arbitrarily.
    """
    known = schema.all_fields()
    if raw in known:
        return FieldResolution(original=raw, resolved=raw, candidates=[])

    normalized_map = {_normalize(f): f for f in known}
    norm = _normalize(raw)
    if norm in normalized_map:
        return FieldResolution(original=raw, resolved=normalized_map[norm], candidates=[])

    scored = sorted(
        ((difflib.SequenceMatcher(None, norm, cand).ratio(), cand) for cand in normalized_map),
        key=lambda pair: pair[0],
        reverse=True,
    )
    above_cutoff = [(score, cand) for score, cand in scored if score >= FUZZY_MATCH_CUTOFF]

    if not above_cutoff:
        return FieldResolution(original=raw, resolved=None, candidates=[])
    if len(above_cutoff) == 1 or above_cutoff[0][0] - above_cutoff[1][0] >= FUZZY_MARGIN:
        winner = normalized_map[above_cutoff[0][1]]
        return FieldResolution(original=raw, resolved=winner, candidates=[winner])

    candidates = [normalized_map[cand] for _, cand in above_cutoff[:3]]
    return FieldResolution(original=raw, resolved=None, candidates=candidates)


@dataclass
class PlanResolutionResult:
    schema_fields: list[str] = _field(default_factory=list)  # de-duplicated, for the JOIN planner
    field_rewrites: dict[str, str] = _field(default_factory=dict)  # original -> resolved, auto-applied
    unresolved: list[FieldResolution] = _field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved


def resolve_query_plan_fields(plan: QueryPlan, schema: SchemaCatalog) -> PlanResolutionResult:
    """Resolve every schema-field reference in `plan` against `schema`.

    `having[].field` and `sort[].field` are skipped when they already match a
    metric's output alias -- those reference an aggregation result, not a
    schema column (see compiler/sql_compiler.py), and are validated there,
    not here.
    """
    metric_aliases = {m.output_alias() for m in plan.metrics}
    result = PlanResolutionResult()

    def check(raw: str, context: str) -> None:
        res = resolve_field_name(raw, schema)
        if res.resolved is None:
            result.unresolved.append(FieldResolution(res.original, None, res.candidates, context))
            return
        if res.resolved != raw:
            result.field_rewrites[raw] = res.resolved
        if res.resolved not in result.schema_fields:
            result.schema_fields.append(res.resolved)

    for i, m in enumerate(plan.metrics):
        check(m.field, f"metrics[{i}].field")
    for i, d in enumerate(plan.dimensions):
        check(d, f"dimensions[{i}]")
    for i, f in enumerate(plan.filters):
        check(f.field, f"filters[{i}].field")
    if plan.time_range is not None:
        check(plan.time_range.field, "time_range.field")
    for i, mc in enumerate(plan.metric_comparisons):
        check(mc.left.field, f"metric_comparisons[{i}].left.field")
        check(mc.right.field, f"metric_comparisons[{i}].right.field")
    for i, hf in enumerate(plan.having):
        if hf.field not in metric_aliases:
            check(hf.field, f"having[{i}].field")
    for i, s in enumerate(plan.sort):
        if s.field not in metric_aliases:
            check(s.field, f"sort[{i}].field")

    return result


def apply_field_rewrites(plan: QueryPlan, rewrites: dict[str, str]) -> QueryPlan:
    """Return a deep copy of `plan` with every schema-field reference rewritten
    per `rewrites`. Fields not present in `rewrites` are left unchanged.
    """
    if not rewrites:
        return plan
    new_plan = plan.model_copy(deep=True)
    for m in new_plan.metrics:
        m.field = rewrites.get(m.field, m.field)
    new_plan.dimensions = [rewrites.get(d, d) for d in new_plan.dimensions]
    for f in new_plan.filters:
        f.field = rewrites.get(f.field, f.field)
    if new_plan.time_range is not None:
        new_plan.time_range.field = rewrites.get(new_plan.time_range.field, new_plan.time_range.field)
    for mc in new_plan.metric_comparisons:
        mc.left.field = rewrites.get(mc.left.field, mc.left.field)
        mc.right.field = rewrites.get(mc.right.field, mc.right.field)
    for hf in new_plan.having:
        hf.field = rewrites.get(hf.field, hf.field)
    for s in new_plan.sort:
        s.field = rewrites.get(s.field, s.field)
    return new_plan
