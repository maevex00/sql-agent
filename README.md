# sql-agent

A Slack analytics agent built around one design principle: **the LLM understands business
intent; deterministic code decides how enterprise data is queried and executed.**

The LLM never writes or edits SQL. It produces a typed `QueryPlan` (metrics, dimensions,
filters, time range, etc.); everything after that — resolving which tables own which
fields, finding a correct JOIN path through the schema graph, compiling SQL, and enforcing
read-only/limit/timeout safety — is deterministic and unit-tested independently of any LLM
call.

Full design rationale, including several real bugs the test suite caught and fixed during
development, is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

Phases 0-5 are implemented and unit-tested (71 tests, `make test`, no database or API key
required to run them). **No Slack integration yet.**

- [schema/correlation.json](schema/correlation.json) — 13-table demo schema (generic
  retail/e-commerce domain, not tied to any company), with intentional multi-hop chains and
  branching paths to exercise the JOIN planner.
- [src/joinplanner/](src/joinplanner) — symmetric-graph BFS + conditional greedy table-cover
  selection, returning a structured `JoinPlan` (not a SQL string).
- [src/planner/query_plan.py](src/planner/query_plan.py) — the `QueryPlan` semantic IR.
- [src/planner/date_resolver.py](src/planner/date_resolver.py) — pure function resolving
  relative time periods ("last quarter") to concrete dates.
- [src/compiler/sql_compiler.py](src/compiler/sql_compiler.py) — `QueryPlan` + `JoinPlan` →
  `sqlglot` AST → SQL text, with no string-formatting of any derived value.
- [src/safety/guard.py](src/safety/guard.py) — AST-level read-only enforcement + LIMIT
  clamping, independent of the Postgres read-only role that is the second line of defense.
- [src/resolver/schema_resolver.py](src/resolver/schema_resolver.py) — margin-based fuzzy
  field-name resolution against the schema catalog.
- [src/planner/llm_planner.py](src/planner/llm_planner.py) — NL → `QueryPlan` via a forced
  Anthropic tool-use call; the parsing/validation half is unit-tested independently of the
  network call.
- [src/planner/repair.py](src/planner/repair.py) — the three-mechanism Repair Loop
  orchestrator (structural / schema-resolution / semantic-replan), tested with injected fake
  LLM callables.
- [scripts/ask.py](scripts/ask.py) — CLI: ask a question, get compiled + guarded SQL.
  Requires `ANTHROPIC_API_KEY`; not exercised by the test suite for that reason.

Not yet built: Intent Router, Glossary injection, Result Analyzer, Slack integration, Eval
harness. See ARCHITECTURE.md's roadmap for phases 6-10.

## Quick start (deterministic core + repair-loop tests, no DB or API key needed)

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest -v
```

## Ask a real question (requires an Anthropic API key)

```bash
export ANTHROPIC_API_KEY=sk-...
python scripts/ask.py "revenue by region for completed orders last quarter"
```

## Full stack (requires Docker)

```bash
make setup   # docker compose up + seed synthetic data
make test
```
