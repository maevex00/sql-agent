"""QueryPlan: the semantic IR between natural language and SQL.

This is the one artifact both the LLM and the deterministic pipeline agree on.
The LLM only ever produces/repairs a QueryPlan; nothing downstream of it lets
the LLM touch SQL text. See ARCHITECTURE.md section 1 for the design rationale
behind each field, in particular why `having` and `metric_comparisons` are two
narrow shapes instead of one generic post-aggregation expression, and why
`TimeRange` distinguishes absolute from relative (relative periods are
resolved by a pure function, never by the LLM).
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class Filter(BaseModel):
    field: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not_in", "like", "between"]
    value: Union[str, int, float, list[Any]]


class Aggregation(BaseModel):
    field: str
    func: Literal["sum", "avg", "count", "count_distinct", "min", "max"]
    alias: str | None = None

    def output_alias(self) -> str:
        return self.alias or f"{self.func}_{self.field}"


class MetricComparison(BaseModel):
    """Post-aggregation metric-to-metric comparison, e.g. SUM(actual) < SUM(target)."""

    left: Aggregation
    op: Literal["=", "!=", ">", ">=", "<", "<="]
    right: Aggregation


class AbsoluteTimeRange(BaseModel):
    kind: Literal["absolute"] = "absolute"
    field: str
    start: date
    end: date


class RelativeTimeRange(BaseModel):
    """The LLM only ever picks `period` (+ `n` for last_n_days). Converting this
    to concrete dates is the Date Resolver's job (planner/date_resolver.py), a
    pure function of (period, now) -- never left to the LLM.
    """

    kind: Literal["relative"] = "relative"
    field: str
    period: Literal[
        "last_n_days",
        "this_week",
        "last_week",
        "this_month",
        "last_month",
        "this_quarter",
        "last_quarter",
        "this_year",
        "last_year",
        "month_to_date",
        "quarter_to_date",
        "year_to_date",
    ]
    n: int | None = None


TimeRange = Annotated[Union[AbsoluteTimeRange, RelativeTimeRange], Field(discriminator="kind")]


class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "desc"


class QueryPlan(BaseModel):
    metrics: list[Aggregation]
    dimensions: list[str] = []
    filters: list[Filter] = []
    having: list[Filter] = []
    metric_comparisons: list[MetricComparison] = []
    time_range: TimeRange | None = None
    sort: list[SortSpec] = []
    limit: int = Field(default=100, le=10_000)
