"""Loads eval/benchmark.jsonl into typed BenchmarkCase objects.

Pure and testable: no LLM, no DB, just JSON -> Pydantic. See
generate_benchmark.py for how the file itself is produced, and
tests/test_benchmark_integrity.py for the (also pure, no-LLM) check that
every case's expected_plan actually resolves against the real schema.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from planner.query_plan import QueryPlan
from router.intent_router import IntentLabel

BENCHMARK_PATH = Path(__file__).parent / "benchmark.jsonl"

Category = str  # simple_aggregation / multi_table_join / time_series / multi_filter /
# ambiguous_terminology / unsupported_unsafe -- kept as plain str rather than
# a Literal so adding a category doesn't require touching this module.


class BenchmarkCase(BaseModel):
    id: str
    category: Category
    question: str
    expected_intent: IntentLabel = "ANALYTICS_QUERY"
    expected_plan: QueryPlan | None = None


def load_benchmark(path: str | Path = BENCHMARK_PATH) -> list[BenchmarkCase]:
    cases = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(BenchmarkCase.model_validate_json(line))
    return cases
