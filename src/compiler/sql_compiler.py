"""QueryPlan + JoinPlan -> sqlglot AST -> SQL text.

No string formatting of any QueryPlan-derived value happens anywhere in this
module. Every value becomes an `exp.Literal` node and every identifier an
`exp.Column`/`exp.Table` node; `sqlglot` renders (and escapes) them when the
AST is turned into text. This is what makes the compiler deterministic and
injection-safe by construction rather than by discipline.

`time_range` must already be an `AbsoluteTimeRange` by the time it reaches
here -- resolving a `RelativeTimeRange` is `planner.date_resolver`'s job, not
the compiler's.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from joinplanner.models import JoinPlan
from planner.query_plan import AbsoluteTimeRange, Aggregation, Filter, MetricComparison, QueryPlan

_COMPARISON_OPS: dict[str, type[exp.Expression]] = {
    "=": exp.EQ,
    "!=": exp.NEQ,
    ">": exp.GT,
    ">=": exp.GTE,
    "<": exp.LT,
    "<=": exp.LTE,
}

_AGG_FUNCS: dict[str, type[exp.Expression]] = {
    "sum": exp.Sum,
    "avg": exp.Avg,
    "min": exp.Min,
    "max": exp.Max,
    "count": exp.Count,
}


def _col(alias: str, field: str) -> exp.Column:
    return exp.column(field, table=alias)


def _lit(value) -> exp.Expression:
    if isinstance(value, bool):
        return exp.true() if value else exp.false()
    if isinstance(value, (int, float)):
        return exp.Literal.number(str(value))
    return exp.Literal.string(str(value))


def _condition(column_expr: exp.Expression, op: str, value) -> exp.Expression:
    if op == "in":
        return exp.In(this=column_expr, expressions=[_lit(v) for v in value])
    if op == "not_in":
        return exp.Not(this=exp.In(this=column_expr, expressions=[_lit(v) for v in value]))
    if op == "like":
        return exp.Like(this=column_expr, expression=_lit(value))
    if op == "between":
        lo, hi = value
        return exp.Between(this=column_expr, low=_lit(lo), high=_lit(hi))
    if op in _COMPARISON_OPS:
        return _COMPARISON_OPS[op](this=column_expr, expression=_lit(value))
    raise ValueError(f"Unsupported filter operator: {op}")


def _compare_exprs(left: exp.Expression, op: str, right: exp.Expression) -> exp.Expression:
    if op not in _COMPARISON_OPS:
        raise ValueError(f"Unsupported comparison operator: {op}")
    return _COMPARISON_OPS[op](this=left, expression=right)


def _aggregation_expr(agg: Aggregation, field_alias: dict[str, str]) -> exp.Expression:
    col = _col(field_alias[agg.field], agg.field)
    if agg.func == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[col]))
    return _AGG_FUNCS[agg.func](this=col)


def _metric_by_output_alias(metrics: list[Aggregation], alias: str) -> Aggregation:
    for m in metrics:
        if m.output_alias() == alias:
            return m
    raise ValueError(
        f"having/metric_comparisons references unknown metric alias '{alias}' "
        f"-- must match one of {[m.output_alias() for m in metrics]}"
    )


def _table_expr(table: str, alias: str) -> exp.Expression:
    return exp.alias_(exp.to_table(table), alias, table=True)


def compile_sql(
    plan: QueryPlan,
    join_plan: JoinPlan,
    field_table: dict[str, str],
    *,
    dialect: str = "postgres",
    max_limit: int = 10_000,
) -> str:
    """Render a QueryPlan + resolved JoinPlan into SQL text.

    :param field_table: field name -> owning table name (from
        joinplanner.sql_builder.plan_join_for_fields), used together with
        join_plan.table_alias to qualify every column reference.
    :param max_limit: server-side hard cap, independent of plan.limit -- see
        ARCHITECTURE.md, SQL Safety Layer.
    """
    if plan.time_range is not None and not isinstance(plan.time_range, AbsoluteTimeRange):
        raise ValueError("compile_sql requires an already-resolved AbsoluteTimeRange")

    field_alias = {field: join_plan.table_alias[table] for field, table in field_table.items()}

    select_exprs: list[exp.Expression] = [
        exp.alias_(_col(field_alias[dim], dim), dim) for dim in plan.dimensions
    ]
    select_exprs += [
        exp.alias_(_aggregation_expr(m, field_alias), m.output_alias()) for m in plan.metrics
    ]

    steps = join_plan.steps
    query = sqlglot.select(*select_exprs).from_(_table_expr(steps[0].table, steps[0].alias))
    for step in steps[1:]:
        on = exp.EQ(
            this=_col(step.on_left_alias, step.on_left_key),
            expression=_col(step.alias, step.on_right_key),
        )
        query = query.join(_table_expr(step.table, step.alias), on=on, join_type="LEFT")

    for f in plan.filters:
        query = query.where(_condition(_col(field_alias[f.field], f.field), f.op, f.value))

    if plan.time_range is not None:
        tr = plan.time_range
        col = _col(field_alias[tr.field], tr.field)
        query = query.where(exp.Between(this=col, low=_lit(str(tr.start)), high=_lit(str(tr.end))))

    if plan.dimensions:
        query = query.group_by(*(_col(field_alias[dim], dim) for dim in plan.dimensions))

    for hf in plan.having:
        metric = _metric_by_output_alias(plan.metrics, hf.field)
        query = query.having(_condition(_aggregation_expr(metric, field_alias), hf.op, hf.value))

    for mc in plan.metric_comparisons:
        left_expr = _aggregation_expr(mc.left, field_alias)
        right_expr = _aggregation_expr(mc.right, field_alias)
        query = query.having(_compare_exprs(left_expr, mc.op, right_expr))

    for s in plan.sort:
        order_col = _col(field_alias[s.field], s.field) if s.field in field_alias else exp.column(s.field)
        query = query.order_by(order_col.desc() if s.direction == "desc" else order_col.asc())

    limit = min(plan.limit, max_limit)
    query = query.limit(limit)

    return query.sql(dialect=dialect, pretty=True)
