# sql-agent

A hybrid LLM + deterministic-algorithm analytics agent that answers natural-language
business questions over a SQL database — built around one specific, well-known failure mode
in text-to-SQL systems: **LLMs hallucinate JOINs because they don't reliably know how tables
in a real schema actually relate to each other.**

**Core design principle:** the LLM understands business intent; deterministic code decides
how enterprise data is queried and executed. The LLM never writes or edits SQL — it only
ever produces a typed `QueryPlan` (metrics, dimensions, filters, time range, post-aggregation
conditions). Everything after that — resolving which tables own which fields, finding a
correct JOIN path through the schema graph, compiling SQL, and enforcing
read-only/limit/timeout safety — is deterministic, unit-tested, and independent of any LLM
call.

```
Slack (Socket Mode) ─┐
                      ▼
scripts/ask.py (CLI) → Intent Router → Glossary → LLM Query Planner → Schema Resolver
                                                          │
                                              (Repair Loop: structural /
                                               schema-resolution / semantic-replan)
                                                          ▼
                                     Graph JOIN Planner (BFS + conditional greedy)
                                                          ▼
                                     Deterministic SQL Compiler (sqlglot AST)
                                                          ▼
                                          SQL Safety Layer (AST denylist,
                                          LIMIT/timeout, read-only DB role)
                                                          ▼
                                              PostgreSQL (read-only)
                                                          ▼
                                    Result Analyzer (chart + LLM summary,
                                          no SQL access) → answer
```

Full design rationale — including every real bug the test suite caught during development,
what broke, and how it was fixed — is in [ARCHITECTURE.md](ARCHITECTURE.md).

## What makes this more than an NL2SQL demo

- **Graph-based JOIN planning**, not LLM guesswork. A symmetric-BFS shortest-path search
  over the schema's FK graph, with a conditional greedy set-cover step for genuinely
  ambiguous field→table mappings — see ARCHITECTURE.md for why topological sort was
  *removed* (it assumed acyclic schemas and added risk with no benefit) and why the greedy
  step's trigger condition changed mid-development after a test caught it hiding real
  ambiguity.
- **A closed IR that structurally cannot express a mutation.** `QueryPlan` has no DDL/DML
  concept at all — there's no field an LLM could fill in that produces `DROP TABLE`. The
  AST-level SQL Safety Layer is defense-in-depth on top of that, not the only thing standing
  between a request and a destructive query.
- **Three distinct repair mechanisms**, not one generic "ask the LLM to fix it" loop:
  structural (bad JSON), schema-resolution (unknown field, constrained candidate list), and
  semantic-replan (the plan is valid but genuinely unanswerable as asked) — each with its own
  attempt budget plus a shared ceiling bounding worst-case latency.
- **A benchmark-integrity test suite** that validates every eval ground-truth case against
  the real schema *before* a single LLM call is spent on it.
- **Honest, not padded, scope decisions**, documented where they happened: the eval
  benchmark shipped at 60 cases instead of the originally-targeted 80-100 because that's what
  could actually be verified in this environment; the Slack integration is flagged as
  code-complete-but-unverified rather than presented as tested.

## Status

All 9 planned pipeline phases are code-complete; only final polish remains. Phases 0-7 and 9
are unit-tested (122 tests, `make test`, no database or API key required to run them).
Phase 8 (Slack) is **honestly unverified** — this dev environment has no Slack app/workspace
to test against.

| Stage | Module | Notes |
|---|---|---|
| Schema | [schema/correlation.json](schema/correlation.json) | 13-table generic retail/e-commerce demo schema, multi-hop + branching by design |
| JOIN planning | [src/joinplanner/](src/joinplanner) | Symmetric BFS + conditional greedy → structured `JoinPlan`, not a SQL string |
| Semantic IR | [src/planner/query_plan.py](src/planner/query_plan.py) | `QueryPlan` — the only thing the LLM ever produces |
| Relative time | [src/planner/date_resolver.py](src/planner/date_resolver.py) | Pure function: `"last_quarter"` + a reference date → concrete dates |
| SQL Compiler | [src/compiler/sql_compiler.py](src/compiler/sql_compiler.py) | `QueryPlan`+`JoinPlan` → `sqlglot` AST → SQL, no string-formatted values anywhere |
| Safety | [src/safety/guard.py](src/safety/guard.py) | AST mutation-denylist walk + LIMIT/timeout, independent of the read-only DB role |
| Field resolution | [src/resolver/schema_resolver.py](src/resolver/schema_resolver.py) | Margin-based fuzzy matching against the schema catalog |
| LLM planning | [src/planner/llm_planner.py](src/planner/llm_planner.py) | NL → `QueryPlan` via forced tool-use; parsing is tested, the live call isn't |
| Repair loop | [src/planner/repair.py](src/planner/repair.py) | Structural / schema-resolution / semantic-replan orchestration, tested with fake LLM callables |
| Intent routing | [src/router/intent_router.py](src/router/intent_router.py) | `ANALYTICS_QUERY` / `METRIC_DEFINITION` / `UNSUPPORTED` |
| Glossary | [glossary/](glossary) + [src/planner/glossary.py](src/planner/glossary.py) | Business vocabulary injected into the LLM prompt; answers `METRIC_DEFINITION` directly, no SQL |
| DB execution | [src/db/postgres.py](src/db/postgres.py) | Executes only already-guarded SQL, read-only role |
| Result analysis | [src/report/analyzer.py](src/report/analyzer.py) | Headline + chart + LLM summary — summary is grounded in the question/plan/rows, never the SQL |
| Orchestration | [src/service.py](src/service.py) | `answer_question()` — the one function both front-ends call |
| CLI | [scripts/ask.py](scripts/ask.py) | Thin front-end for `service.answer_question()` |
| Slack | [src/slack/](src/slack) | `formatting.py` pure & tested; `app.py`'s live wiring is unverified (see caveat below) |
| Eval | [eval/](eval) | 60 benchmark cases, pure scoring functions, benchmark-integrity tests |

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
Phase 8 notes for exactly why and what that means.

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) in a workspace you
   control. Enable **Socket Mode** (no public URL needed) and generate an app-level token
   with the `connections:write` scope → `SLACK_APP_TOKEN`.
2. Add bot token scopes `chat:write`, `app_mentions:read`, `im:history`, `files:write`,
   install the app, and copy the bot token → `SLACK_BOT_TOKEN`.
3. `export SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... ANTHROPIC_API_KEY=...` and run:

```bash
python src/slack/app.py
```

## Run the eval harness (requires an Anthropic API key; DATABASE_URL optional)

```bash
export ANTHROPIC_API_KEY=sk-...
python eval/run_eval.py
```

## Full stack (requires Docker)

```bash
make setup   # docker compose up (Postgres + read-only role) + seed synthetic data
make test
```

## License

[MIT](LICENSE)
