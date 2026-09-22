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

Phases 0-8 are code-complete. Phases 0-7 are unit-tested (97 tests, `make test`, no database
or API key required to run them). Phase 8 (Slack) is **honestly unverified** — this dev
environment has no Slack app/workspace to test against; see the caveat below.

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
- [src/router/intent_router.py](src/router/intent_router.py) — 3-way classification
  (`ANALYTICS_QUERY` / `METRIC_DEFINITION` / `UNSUPPORTED`) gating everything downstream.
- [glossary/](glossary) + [src/planner/glossary.py](src/planner/glossary.py) — business
  terminology and metric definitions injected into the LLM's prompt; `METRIC_DEFINITION`
  questions are answered directly from this, with no SQL involved.
- [src/db/postgres.py](src/db/postgres.py) — executes already-guarded SQL against the
  read-only role and fetches results; never accepts raw user/LLM SQL as input.
- [src/report/analyzer.py](src/report/analyzer.py) — headline + optional bar chart
  (matplotlib) + LLM prose summary. The summary call is grounded in the question, the
  `QueryPlan`, and result rows only — never the SQL text, keeping the LLM's footprint
  narrow all the way to the last stage.
- [src/service.py](src/service.py) — the shared orchestration both front-ends call:
  `answer_question()` ties Intent Router → Glossary → LLM Query Planner → Repair Loop →
  Postgres execution → Result Analyzer into one function.
- [scripts/ask.py](scripts/ask.py) — CLI front-end for `service.answer_question()`.
  Requires `ANTHROPIC_API_KEY`; not exercised by the test suite for that reason.
- [src/slack/](src/slack) — Bolt/Socket Mode front-end for the same `answer_question()`.
  `formatting.py` (Block Kit construction, mention stripping) is pure and tested;
  `app.py`'s actual Slack wiring is **not verified** — `slack_bolt.App()` makes a live call
  to Slack's `auth.test` endpoint just to construct the object, so there was no way to
  smoke-test it without a real bot token. See ARCHITECTURE.md's Phase 8 notes for what
  that means concretely and what's needed to actually run it.

Not yet built: Eval harness. See ARCHITECTURE.md's roadmap for phases 9-10.

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

## Run the Slack bot (requires a Slack app + an Anthropic API key)

Unverified in this dev environment (no Slack credentials available) — see ARCHITECTURE.md's
Phase 8 notes.

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) in a workspace you
   control. Enable **Socket Mode** (no public URL needed) and generate an app-level token
   with the `connections:write` scope → `SLACK_APP_TOKEN`.
2. Add bot token scopes `chat:write`, `app_mentions:read`, `im:history`, `files:write`,
   install the app, and copy the bot token → `SLACK_BOT_TOKEN`.
3. `export SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... ANTHROPIC_API_KEY=...` and run:

```bash
python src/slack/app.py
```

## Full stack (requires Docker)

```bash
make setup   # docker compose up + seed synthetic data
make test
```
