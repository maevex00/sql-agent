# sql-agent

A Slack analytics agent built around one design principle: **the LLM understands business
intent; deterministic code decides how enterprise data is queried and executed.**

The LLM never writes or edits SQL. It produces a typed `QueryPlan` (metrics, dimensions,
filters, time range, etc.); everything after that — resolving which tables own which
fields, finding a correct JOIN path through the schema graph, compiling SQL, and enforcing
read-only/limit/timeout safety — is deterministic and unit-tested independently of any LLM
call.

Full design rationale, including two real bugs the test suite caught and fixed during
development, is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

Phases 0-4 (deterministic core) are implemented and unit-tested with **no LLM and no Slack
in the loop yet**:

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
- 48 unit tests, `make test`, all passing without a database connection.

Not yet built: LLM Query Planner, Schema Resolver + Repair Loop, Intent Router, Slack
integration, Result Analyzer, Eval harness. See ARCHITECTURE.md's roadmap for phases 5-10.

## Quick start (deterministic core only, no DB needed)

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest -v
```

## Full stack (requires Docker)

```bash
make setup   # docker compose up + seed synthetic data
make test
```
