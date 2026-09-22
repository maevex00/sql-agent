"""Result Analyzer: query result rows -> headline + chart + prose summary.

The LLM's role here is as narrow as everywhere else in the pipeline: it sees
the original question, the QueryPlan (for context on what was asked), and
the RESULT DATA -- never the SQL text. This keeps the "LLM understands
intent, deterministic code decides execution" boundary intact through the
LAST stage too, not just query generation.

`describe_result_shape`, `is_chartable`, and `render_chart` are pure/local
(no network) and unit-tested against hand-built rows. `summarize_result` is
the one live-LLM call in this module and is not covered by the test suite,
same pattern as llm_planner.plan_query / intent_router.classify_intent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from planner.query_plan import QueryPlan


@dataclass
class AnalysisResult:
    headline: str
    chart_path: str | None
    summary: str


def describe_result_shape(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """Deterministic one-line description of the result set. Used as the
    fallback headline/summary when the LLM call is skipped, and as grounding
    context fed INTO the LLM summary prompt so it doesn't have to re-derive
    basic facts (row count, columns) by eye from a large row list.
    """
    if not rows:
        return "No rows returned."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0]} = {rows[0][0]}"
    return f"{len(rows)} rows across {len(columns)} columns: {', '.join(columns)}."


def is_chartable(plan: QueryPlan, columns: list[str], rows: list[tuple[Any, ...]]) -> bool:
    """A single dimension with one or more metrics, and a small enough row
    count to read as a bar chart, is chartable. Anything else (no dimension,
    multiple dimensions, too many rows) is left as a table -- charting those
    well needs chart-type-selection logic this project doesn't need yet.
    """
    if not rows or len(rows) > 25:
        return False
    return len(plan.dimensions) == 1 and len(plan.metrics) >= 1


def render_chart(plan: QueryPlan, columns: list[str], rows: list[tuple[Any, ...]], out_path: str | Path) -> str:
    """Grouped bar chart: one group per dimension value, one bar per metric.
    Only meaningful when is_chartable() is True -- callers are expected to
    check that first.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dim_index = columns.index(plan.dimensions[0])
    metric_aliases = [m.output_alias() for m in plan.metrics]
    metric_indexes = [columns.index(alias) for alias in metric_aliases]

    labels = [str(row[dim_index]) for row in rows]
    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 0.6), 4))
    width = 0.8 / len(metric_indexes)
    x = range(len(labels))
    for i, (alias, idx) in enumerate(zip(metric_aliases, metric_indexes)):
        values = [row[idx] for row in rows]
        ax.bar([xi + i * width for xi in x], values, width=width, label=alias)
    ax.set_xticks([xi + width * (len(metric_indexes) - 1) / 2 for xi in x])
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.legend()
    fig.tight_layout()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return str(out_path)


SUMMARY_SYSTEM_PROMPT = """Summarize the query result for the user in 2-4 sentences of plain
prose. You are given the original question, which fields were aggregated/grouped, and the
result rows -- never the SQL that produced them. Do not speculate about data you were not
given, and do not describe how the query was constructed."""


def summarize_result(
    question: str,
    plan: QueryPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    model: str = "claude-sonnet-5",
    api_key: str | None = None,
) -> str:
    """LLM prose summary grounded in result data only -- no SQL access. Not
    covered by unit tests (live API call); see module docstring.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    row_preview = rows[:20]  # cap prompt size regardless of result size
    content = (
        f"Question: {question}\n"
        f"Dimensions: {plan.dimensions}\n"
        f"Metrics: {[m.output_alias() for m in plan.metrics]}\n"
        f"Result shape: {describe_result_shape(columns, rows)}\n"
        f"Columns: {columns}\n"
        f"Rows (first {len(row_preview)} of {len(rows)}): {row_preview}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return next(block.text for block in response.content if block.type == "text")


def analyze(
    question: str,
    plan: QueryPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    chart_dir: str | Path = "output",
    use_llm_summary: bool = True,
) -> AnalysisResult:
    """Orchestrates headline + optional chart + prose summary.

    `use_llm_summary=False` skips the live LLM call and falls back to
    describe_result_shape() as the summary -- this is what makes the
    orchestration itself testable without an API key (tests/test_analyzer.py).
    """
    headline = describe_result_shape(columns, rows)
    chart_path = None
    if is_chartable(plan, columns, rows):
        chart_path = render_chart(plan, columns, rows, Path(chart_dir) / "result.png")

    summary = summarize_result(question, plan, columns, rows) if use_llm_summary else headline
    return AnalysisResult(headline=headline, chart_path=chart_path, summary=summary)
