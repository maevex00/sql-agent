#!/usr/bin/env python
"""CLI: ask a natural-language analytics question, get an answer.

Thin wrapper around service.answer_question() -- see that module for the
actual pipeline wiring (Intent Router -> Glossary -> LLM Query Planner ->
Repair Loop -> Postgres execution -> Result Analyzer). This script and
slack/app.py are the two front-ends that share it.

Requires ANTHROPIC_API_KEY. Not covered by the test suite (live LLM/DB
calls) -- see tests/test_repair.py, tests/test_intent_router.py,
tests/test_analyzer.py for the orchestration logic this wraps, tested with
fake callables and hand-built inputs.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from joinplanner.sql_builder import SchemaCatalog  # noqa: E402
from planner.glossary import load_glossary  # noqa: E402
from service import answer_question  # noqa: E402

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


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

    outcome = answer_question(args.question, schema, glossary_text)

    print(f"--- intent: {outcome.intent} ---", file=sys.stderr)
    if outcome.repair_log:
        print("--- repair log ---", file=sys.stderr)
        for line in outcome.repair_log:
            print(f"  {line}", file=sys.stderr)
    if outcome.chart_path:
        print(f"--- chart saved to {outcome.chart_path} ---", file=sys.stderr)

    print(outcome.text)
    if outcome.intent == "UNSUPPORTED" or outcome.error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
