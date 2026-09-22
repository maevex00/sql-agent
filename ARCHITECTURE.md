# Architecture Baseline

Status: **FINAL — v1 scope frozen.** Changes require an explicit new decision, not incremental scope creep.

## Design Principle

> LLM understands business intent; deterministic components decide how enterprise data is
> queried and executed.

Every pipeline stage after the LLM Query Planner is deterministic, unit-testable, and
replayable from a logged `QueryPlan`. The LLM's only two touchpoints are: (1) producing a
`QueryPlan` from natural language, and (2) summarizing a result set in prose. It never
produces or edits SQL text.

## Pipeline

```
Slack (Socket Mode / Bolt)
        |  thread_ts identifies conversation context
        v
1. Intent Router            ANALYTICS_QUERY / METRIC_DEFINITION / UNSUPPORTED
        v
2. Glossary Layer            business terms + metric catalog injected into planner prompt
        v
3. LLM Query Planner          NL -> QueryPlan (Pydantic structured output)
        v
4. Schema / Field Resolver    semantic field refs -> table.column, synonym matching
        v
5. Graph JOIN Planner          BFS shortest path (default); greedy anchor selection only
                               when >=3 target tables are involved
        v
6. Deterministic SQL Compiler  QueryPlan + JoinPlan -> sqlglot AST -> SQL text (postgres)
        v
7. SQL Safety Layer            read-only AST check + LIMIT + timeout + row cap
        v
   PostgreSQL (read-only role)
        v
8. Result Analyzer             metrics + chart + LLM prose summary (no SQL access)
        v
   Slack response

[failure at any stage] -> Repair Loop (structural / schema-resolution / semantic-replan)
```

## Scope decision: PostgreSQL only

No SQLite backend. This is a portfolio/interview project, not a database-compatibility
demo — maintaining two backends adds engineering surface area with no signal value for the
stated goal (FDE / AI engineer interviews). `docker compose up` is the only setup path,
local and "production-like" are the same environment.

---

## 1. QueryPlan — the semantic IR

```python
class Filter(BaseModel):
    field: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not_in", "like", "between"]
    value: str | int | float | list[Any]

class Aggregation(BaseModel):
    field: str
    func: Literal["sum", "avg", "count", "count_distinct", "min", "max"]
    alias: str | None = None

class MetricComparison(BaseModel):
    """metric-to-metric comparison, e.g. SUM(actual) < SUM(target)"""
    left: Aggregation
    op: Literal["=", "!=", ">", ">=", "<", "<="]
    right: Aggregation

class AbsoluteTimeRange(BaseModel):
    kind: Literal["absolute"] = "absolute"
    field: str
    start: date
    end: date

class RelativeTimeRange(BaseModel):
    kind: Literal["relative"] = "relative"
    field: str
    period: Literal[
        "last_n_days", "this_week", "last_week",
        "this_month", "last_month", "this_quarter", "last_quarter",
        "this_year", "last_year",
        "month_to_date", "quarter_to_date", "year_to_date",
    ]
    n: int | None = None  # only used when period == "last_n_days"

TimeRange = Annotated[AbsoluteTimeRange | RelativeTimeRange, Field(discriminator="kind")]

class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "desc"

class QueryPlan(BaseModel):
    metrics: list[Aggregation]
    dimensions: list[str] = []
    filters: list[Filter] = []                      # pre-aggregation (WHERE)
    having: list[Filter] = []                        # post-aggregation, metric alias vs literal
    metric_comparisons: list[MetricComparison] = []   # post-aggregation, metric vs metric
    time_range: TimeRange | None = None
    sort: list[SortSpec] = []
    limit: int = Field(default=100, le=10_000)        # server enforces the hard cap regardless
```

Key decisions:

- **`having` vs `metric_comparisons`** are kept as two distinct, narrow shapes instead of one
  generic "post-aggregation expression" field. A generic boolean-expression AST would let the
  LLM emit arbitrary logic and reopen the exact problem this whole design avoids (LLM
  producing unconstrained query logic). Two closed shapes cover "Which regions missed their
  sales targets?" (`metric_comparisons: [SUM(actual_sales) < SUM(sales_target)]`) without a
  general expression grammar.
- **Relative time is never resolved by the LLM.** The LLM only picks a `period` enum value
  (+ `n` for `last_n_days`). A pure function, `resolver/date_resolver.py:resolve(time_range,
  now) -> AbsoluteTimeRange`, converts it to concrete dates. This function takes `now` as an
  explicit argument (never reads the clock itself), which makes it a pure, deterministic,
  fully unit-testable component — "last quarter" as of a fixed date has exactly one correct
  answer, and that answer must not depend on LLM sampling.
- **`limit` is proposed by the plan but never trusted.** The safety layer clamps it to a
  server-side hard ceiling independent of what the LLM asked for.

---

## 2. Graph JOIN Planner — algorithm choices, justified

### Topological sort: removed

The original prototype used topological sort purely to get a stable iteration order over
candidate tables for the greedy anchor-selection step — not because any part of the
algorithm needs DAG semantics. That justification does not hold up:

- Real FK graphs are not guaranteed acyclic (hub tables, bidirectional references, junction
  tables all produce cycles). `topological_sort` throws on a cycle, which means a valid,
  ordinary schema addition could crash query planning in production.
- The property actually needed is *"a fixed, deterministic order to iterate candidate
  tables"* — not *"a linearization consistent with edge direction."*

**Replacement:** `correlation.json` carries an explicit `tables` array (declaration order).
The join planner uses that order as-is; if a table is missing from it, tie-break by sorted
table name. No graph computation, no cycle risk, `O(1)` instead of `O(V+E)` per rebuild.

### BFS: default and primary

`find_join_path_to_target` (shortest-path BFS) is unchanged in spirit but the underlying
graph is now built **symmetrically**. The original implementation only traversed edges in
the direction they were declared in `nextarc` (e.g. `orders -> customers`), which means
path-finding silently failed whenever the algorithm needed to traverse the *reverse*
direction (anchor = `customers`, target = `orders`). Since anchor-table selection is
data-driven (whichever table the greedy step picks), both directions must be traversable.
Fix: when the relation graph is loaded, every edge is inserted in both directions (with
`key`/`next_key` swapped on the reverse edge so the JOIN condition stays correct regardless
of which side is `old_table` in the compiled `ON` clause). Covered by a unit test that
BFS-paths in the direction opposite to `nextarc`'s declared direction.

### Greedy anchor/table-cover selection: conditional, explained

Trigger condition (revised during implementation, see note below): invoked whenever at
least one requested field is owned by **more than one table** — most often a shared FK
column, e.g. `region_id` lives on both `customers` and `regions`. When no field is
ambiguous there is nothing to optimize; each field maps directly to its single owner. This
condition is checked explicitly in code (not implied) so a reviewer can see why the
heuristic did or did not run for a given query.

> **Implementation note.** The original draft of this rule used a raw candidate-table-count
> threshold ("3 or more distinct candidate tables"). Building `tests/test_sql_builder.py`
> against the actual schema caught this hiding real ambiguity: `unit_cost` (owned only by
> `products`) plus `category_id` (owned by both `categories` and `products`) has just 2
> candidate tables, but still poses a genuine "1 table or 2?" choice — and the count-based
> rule picked the 2-table answer instead of consolidating onto `products`. The rule is now
> field-ambiguity-based rather than count-based; see `select_covering_tables()`'s docstring
> in `src/joinplanner/sql_builder.py`.

### Target connection order: nearest-first, not declaration order

When more than one non-anchor table needs to be joined in, they are connected
**nearest-first**: at each step, BFS runs from the current connected set to every
still-unconnected required table, and whichever is currently closest is connected next
(ties broken by schema-declared order). This is recomputed after every connection, because
connecting one required table can shorten — sometimes to zero — the distance to another.

> **Implementation note.** The first draft connected remaining targets in fixed
> schema-declared order. `tests/test_sql_builder.py::test_join_plan_full_six_table_chain`
> caught a case where this produced a wrong-but-working query: connecting `categories`
> before `order_items` routed through `reviews` (a genuinely shorter path from `customers`
> to `products` that has nothing to do with the query), because `order_items` — the table
> that would have provided a direct, on-topic path — hadn't been connected yet. Nearest-first
> avoids this by letting `orders`/`order_items` connect before `categories` is even
> considered, so `categories` reaches the join via `products` (1 hop) instead of the
> 4-hop detour through `reviews`. The resulting SQL was not incorrect (the extra join
> doesn't change the numbers), but it is not the query a reviewer — or a benchmark's
> `correct_join_path_rate` metric — should accept as correct.

### JOIN Planner output: structured, not a string

`SQLBuilder` no longer returns a raw `FROM ... LEFT JOIN ...` string. It returns a
`JoinPlan`:

```python
@dataclass
class JoinStep:
    table: str
    alias: str
    join_type: Literal["FROM", "LEFT JOIN"]
    on_left_alias: str | None      # None for the first (FROM) step
    on_left_key: str | None
    on_right_key: str | None

@dataclass
class JoinPlan:
    steps: list[JoinStep]
    table_alias: dict[str, str]     # table name -> alias, for the compiler's SELECT/WHERE building
```

This is the seam between "how tables connect" (join planner's job, algorithmic) and "how
that becomes SQL text" (compiler's job, `sqlglot`-based) — the two were conflated in the
original prototype and are now separated so each is independently testable.

---

## 3. Deterministic SQL Compiler

Input: `QueryPlan` + `JoinPlan` + field->table resolution map (from the Schema Resolver).
Output: SQL text, postgres dialect.

Builds a `sqlglot.exp.Select` object incrementally:
`SELECT` (dimensions + aggregations with aliases) -> `FROM`/`JOIN` (from `JoinPlan.steps`)
-> `WHERE` (from `filters` + resolved `time_range`) -> `GROUP BY` (dimensions) -> `HAVING`
(from `having` + `metric_comparisons`) -> `ORDER BY` (`sort`) -> `LIMIT` (clamped). No
string formatting of user- or LLM-derived values anywhere — every value is bound through
`sqlglot` expression nodes, which parameterizes/escapes correctly for the target dialect.

---

## 4. SQL Safety Layer

**General principle** (not an MVP-only rule): a query is safe to execute if and only if
its AST contains no mutation/DDL node anywhere in the tree — `Insert`, `Update`, `Delete`,
`Drop`, `Alter`, `Create`, `TruncateTable`, `Grant`, etc. This is checked by walking the
full AST, not by asserting the root node's type. A root-type whitelist (e.g. "root must be
`exp.Select`") would incidentally reject legal read-only constructs like a `WITH` (CTE) or
a `UNION` of two `SELECT`s, which are read-only and should not be treated as unsafe.

**v1 scope limitation, explicitly separate from the rule above:** the Compiler only ever
emits a single `SELECT` (no CTE, no `UNION`) because v1's `QueryPlan` has no construct that
requires one. The guard's top-level check today is therefore `isinstance(ast, exp.Select)`
— but that is a *statement of what v1 produces*, not the definition of "safe." When the
`QueryPlan` grows a construct that needs a CTE or UNION, the guard's AST-walk (mutation
denylist) already covers it; only the top-level allowlist needs to widen from `{Select}` to
`{Select, Union, With}`. This distinction is written into `guard.py`'s docstring so it isn't
lost.

Full checklist, application layer:
1. `sqlglot.parse_one(sql, dialect="postgres")` must succeed.
2. Top-level statement type in the read-only allowlist (`{Select}` in v1).
3. AST walk: no node is an instance of the mutation/DDL denylist.
4. `LIMIT` present and `<=` server hard cap, regardless of what `QueryPlan.limit` requested.
5. Session-level `statement_timeout` set before execution (e.g. 5s).
6. Result fetch capped at N rows / byte size.

Database layer (second, independent line of defense): the application connects as a
Postgres role granted `SELECT` only, created in `schema/ddl_postgres.sql`. Even a bug that
defeats every application-layer check cannot mutate data, because the credential itself
cannot.

---

## 5. Repair Loop — three distinct mechanisms

Failures are not interchangeable and are not all funneled into "ask the LLM to fix the
JSON." Each has a different repair action and its own attempt budget (2 attempts each,
plus a global ceiling of 3 repair cycles per query to bound worst-case latency):

| Type | Trigger | Repair action |
|---|---|---|
| **Structural** | Pydantic/JSON-schema validation fails on raw LLM output | Feed the validation error back, ask only for corrected JSON — no re-reasoning about intent |
| **Schema-resolution** | A field/table reference doesn't resolve against `correlation.json`'s catalog | Resolver proposes a constrained candidate list (exact/fuzzy synonym match); LLM or a high-confidence auto-pick selects **only from that list** — cannot invent a field |
| **Semantic replan** | Plan is structurally and referentially valid but downstream signals a genuine intent mismatch (e.g. no candidate interpretation of the target tables is reachable at all) | Full re-prompt with the original question + why the prior plan failed; LLM re-derives the `QueryPlan` from scratch |

A Safety Layer rejection is **not** treated as a repair case — the compiler is trusted to
only emit safe SQL, so a rejection there is logged as an internal bug and surfaced as an
error, not retried.

---

## 6. Evaluation Harness

`eval/benchmark.jsonl`: questions labeled by category (`simple_aggregation`,
`multi_table_join`, `time_series`, `multi_filter`, `ambiguous_terminology`,
`unsupported_unsafe`), each with a ground-truth `QueryPlan` (not an expected exact SQL
string — too brittle against equivalent rewrites). Originally targeted at 80-100 cases;
shipped with 60 — see Phase 9 implementation notes below for why.

Metrics (`eval/run_eval.py`):

- `executable_sql_rate`
- `correct_join_path_rate`
- semantic accuracy, split by IR component (a single blended score hides which layer is
  actually wrong):
  - `metric_accuracy`
  - `dimension_accuracy`
  - `filter_accuracy`
  - `time_range_accuracy`
  - `execution_accuracy` — compares actual query **results** against a hand-written
    reference SQL's results, not SQL text. This is the most important of the five: two
    different join orders or SQL formulations can be equally correct and produce identical
    results, so text/structure comparison alone would misgrade correct answers as wrong.
- `unsafe_query_blocking_rate` (target: 100% on the `unsupported_unsafe` category)
- `repair_success_rate`, optionally broken down by the three repair types above

---

## Repo Structure

```
sql-agent/
├── ARCHITECTURE.md
├── README.md
├── docker-compose.yml            # postgres service + read-only role bootstrap
├── Makefile                      # setup / eval / run / test
├── pyproject.toml
├── schema/
│   ├── correlation.json          # table_column + nextarc + tables (declared order)
│   └── ddl_postgres.sql          # DDL + read-only role grant
├── data/seed.py                  # Faker seed generator
├── glossary/
│   ├── terms.md
│   └── metrics_catalog.md
├── src/
│   ├── planner/
│   │   ├── query_plan.py         # Pydantic IR
│   │   ├── date_resolver.py      # relative -> absolute time (pure function)
│   │   ├── llm_planner.py        # NL -> QueryPlan (structured output)
│   │   ├── glossary.py           # loads glossary/*.md, answers METRIC_DEFINITION questions
│   │   └── repair.py             # 3-way repair dispatch
│   ├── resolver/schema_resolver.py
│   ├── joinplanner/
│   │   ├── algorithm.py          # BFS (symmetric graph), no topological sort
│   │   ├── sql_builder.py        # anchor/cover selection -> JoinPlan
│   │   └── models.py             # JoinStep / JoinPlan
│   ├── compiler/sql_compiler.py  # QueryPlan+JoinPlan -> sqlglot AST -> SQL
│   ├── safety/guard.py
│   ├── db/postgres.py
│   ├── router/intent_router.py   # 3-way: ANALYTICS_QUERY / METRIC_DEFINITION / UNSUPPORTED
│   ├── report/analyzer.py
│   ├── service.py                # shared orchestration: both scripts/ask.py and slack/app.py call this
│   └── slack/
│       ├── formatting.py         # pure Block Kit construction + mention stripping, tested
│       └── app.py                # Bolt, Socket Mode -- live Slack wiring, unverified (see notes)
├── eval/
│   ├── generate_benchmark.py     # builds benchmark.jsonl from typed QueryPlan objects
│   ├── benchmark.jsonl           # generated, committed -- 60 cases across 6 categories
│   ├── loader.py                 # BenchmarkCase model + JSONL loader
│   ├── scoring.py                # pure scoring functions, no LLM/DB
│   └── run_eval.py               # orchestrator: runs the benchmark live, prints the report
├── scripts/
│   └── ask.py                    # CLI front-end for service.answer_question()
└── tests/
```

---

## Roadmap

| Phase | Content | Milestone | Status |
|---|---|---|---|
| 0 | Scaffold, `QueryPlan` finalized | this document + repo skeleton | done |
| 1 | Multi-hop schema, `correlation.json`, `ddl_postgres.sql`, Faker seed | data available | done |
| 2 | Join engine: symmetric BFS, explicit table order, `JoinPlan` output | unit-tested, no LLM | done |
| 3 | SQL Compiler: hand-built `QueryPlan`+`JoinPlan` -> SQL, unit tests | deterministic core provably correct | done |
| 4 | Safety Layer + adversarial unit tests | safety provably correct | done |
| 5 | LLM Query Planner + Schema Resolver + Repair Loop, CLI end-to-end | first full pipeline milestone | done |
| 6 | Intent Router + Glossary injection | | done |
| 7 | Result Analyzer (metrics + chart + LLM summary) + Postgres execution layer | | done |
| 8 | Slack Bolt / Socket Mode integration | demoable in Slack | code complete, unverified (no Slack credentials in dev environment) |
| 9 | Eval harness, benchmark questions, 5(+) metrics | quantified results | done (60 cases, not 80-100 -- see notes) |
| 10 | README polish, `docker compose up` one-command demo | portfolio-ready | not started |

Phase 5 implementation notes:

- `src/planner/llm_planner.py` — `parse_llm_output` (pure, tested) validates raw tool-call
  JSON against `QueryPlan`; `plan_query` wraps the real Anthropic call and is not exercised
  by the test suite (no API key in the dev/test environment) — see `scripts/ask.py`, the
  actual end-to-end entry point, for the live wiring.
- `src/resolver/schema_resolver.py` — field resolution turned out to need more than a plain
  `difflib.get_close_matches` cutoff: this schema has many `*_name` columns
  (`region_name`, `product_name`, `promotion_name`, ...), so a misspelled `"prodct_name"`
  scored above the cutoff against **both** `product_name` and `promotion_name`. Fixed by
  adding a relative-margin check (`FUZZY_MARGIN`) — the top match must beat the runner-up by
  a clear margin to be accepted as a confident single match; a near-tie is now correctly
  reported as ambiguous (both candidates, `resolved=None`) instead of the resolver guessing.
  Caught by tests/test_schema_resolver.py during development.
- `src/planner/repair.py` — `run_pipeline` is the orchestrator described in "Repair Loop —
  three distinct mechanisms" above, fully unit-tested (tests/test_repair.py) against
  injected fake planner/repair callables, including a test that specifically exercises the
  interaction between each repair type's own budget (`MAX_ATTEMPTS_PER_TYPE = 2`) and the
  shared ceiling (`MAX_TOTAL_REPAIR_CYCLES = 3`).

Phase 6 implementation notes:

- `src/router/intent_router.py` — kept deliberately thin, as scoped: one forced tool-use
  call against a 3-value enum, no repair loop of its own. A malformed classification is
  treated as `UNSUPPORTED` by the caller rather than retried — this stage is cheap enough
  that a wrong classification just means a slightly unhelpful answer, not a broken pipeline.
- `glossary/terms.md` + `glossary/metrics_catalog.md` — generic retail-domain content, not
  tied to any company. Deliberately documents a real limitation instead of hiding it: the
  demo schema has no precomputed revenue/line-total column (`order_items` has `unit_price`
  and `quantity` as separate columns, and `QueryPlan.Aggregation` only wraps a single
  column), so "revenue" resolves to `SUM(unit_price)` with that caveat spelled out in the
  glossary itself — better to flag the simplification than let the LLM or a reader assume
  the number means price × quantity.
- `src/planner/glossary.py` — `load_glossary()` is plain file concatenation, not retrieval;
  per the architecture's stated principle, a retrieval layer would be solving a problem this
  project's small glossary doesn't have. `answer_metric_definition()` grounds its answer in
  the glossary text only, to avoid the LLM inventing a definition the glossary doesn't
  contain.
- `scripts/ask.py` now classifies intent first and only enters the Phase 5 pipeline for
  `ANALYTICS_QUERY`; `METRIC_DEFINITION` is answered directly from the glossary with no SQL
  involved, `UNSUPPORTED` is declined immediately.

Phase 7 implementation notes:

- `src/db/postgres.py` — thin execution wrapper. It never accepts raw user- or LLM-provided
  SQL; its only input contract is "whatever `safety.guard.check_read_only()` already
  approved." Not exercised by the test suite (needs a live Postgres connection); every other
  Phase 7 module is designed so the DB is the *only* untestable piece of this stage.
- `src/report/analyzer.py` — the LLM's footprint here is as narrow as everywhere else in the
  pipeline: `summarize_result()` is given the question, the `QueryPlan` (for context on what
  was aggregated/grouped), and the result rows -- **never the SQL text**. This keeps the
  "LLM understands intent, deterministic code decides execution" boundary intact through the
  *last* stage too, not just query generation. `describe_result_shape`, `is_chartable`, and
  `render_chart` are pure/local (real matplotlib calls, no network) and fully unit-tested,
  including `analyze(..., use_llm_summary=False)`, which exercises the whole orchestration
  (headline + chart decision + fallback summary) without any live call at all.
- Chart scope is deliberately narrow: only a single dimension + one or more metrics, capped
  at 25 rows, renders as a grouped bar chart. Anything else (no dimension, multiple
  dimensions, a large result set) is left as a table rather than guessing at a chart type
  that would need real chart-selection logic this project doesn't need yet.
- `scripts/ask.py` now executes `ANALYTICS_QUERY` SQL against Postgres when `DATABASE_URL`
  is set and reachable, and analyzes the result; otherwise it falls back to printing the
  compiled SQL (the Phase 5 behavior), so the CLI still works end-to-end for demoing the
  deterministic core without Docker running.

Phase 8 implementation notes:

- `src/service.py` was pulled out of `scripts/ask.py` in this phase, not before, because
  that's when a second consumer (`slack/app.py`) of the exact same orchestration
  (Intent Router -> Glossary -> LLM Query Planner -> Repair Loop -> DB execution -> Result
  Analyzer) actually showed up. Factoring it out earlier, before a second caller existed,
  would have been speculative; `scripts/ask.py` is now a thin wrapper that only formats
  `AnswerOutcome` for a terminal.
- `src/slack/formatting.py` is deliberately separated from `src/slack/app.py`: everything
  about *what* gets said (Block Kit construction, stripping the `<@U123ABC>` mention token
  from `app_mention` events) is a pure function, tested in tests/test_slack_formatting.py.
  `app.py` is thin glue that only decides *when* to call them and how to talk to Slack.
- **This phase is honestly unverified**, more so than any earlier one, and worth stating
  plainly rather than glossing over: this development environment has no Slack app or
  workspace, so `src/slack/app.py` has never actually run. It was caught concretely while
  building this phase -- `slack_bolt.App(token=...)` makes a **live network call to Slack's
  `auth.test` endpoint at construction time** to validate the token, so even instantiating
  the `App` object (not starting Socket Mode, just constructing it) requires a real,
  currently-valid bot token. There is no way to smoke-test this module beyond what its pure
  helpers (`strip_mention`, `format_outcome_blocks`) already cover. To actually run it: create
  a Slack app at api.slack.com, enable Socket Mode, grant it `chat:write`,
  `app_mentions:read`, `im:history`, `files:write`, set `SLACK_BOT_TOKEN` /
  `SLACK_APP_TOKEN` / `ANTHROPIC_API_KEY`, and run `python src/slack/app.py`.
- Conversation "context" is thread-based reply only (`thread_ts`), not multi-turn `QueryPlan`
  refinement -- the IR and repair loop have no concept of "adjust the previous query," and
  adding one now would be new scope, not this phase's job. A channel can hold multiple
  concurrent question threads without them interleaving; a single thread does not remember
  earlier questions in it.

Phase 9 implementation notes:

- **Scope reduction from 80-100 cases to 60, stated plainly rather than padded.** Every
  ground-truth case can be *structurally* verified against the real schema right now, with
  no LLM or database: `tests/test_benchmark_integrity.py` resolves every field in every
  `expected_plan` and confirms the join planner can actually connect them. But whether a
  given *question's wording* actually elicits that ground truth needs a live LLM to check,
  and `execution_accuracy` needs a live database on top of that — neither available in this
  dev environment. Sixty cases that are all individually verified this way were judged more
  honest than a larger set padded with content nothing here could check.
- `eval/generate_benchmark.py` builds every case as a real `QueryPlan(...)` object, so
  Pydantic validates its structure at *generation* time, not as a later surprise. The
  generated `eval/benchmark.jsonl` is committed as a plain, diffable file — regenerate it by
  rerunning the script after editing `CASES`, don't hand-edit the JSONL.
- `eval/scoring.py`'s per-component accuracy functions (`metric_accuracy`,
  `dimension_accuracy`, `filter_accuracy`) use Jaccard similarity (partial credit: asking for
  "region and category" when only "region" was expected is a partial match). `correct_join_path`
  deliberately does NOT use Jaccard -- a join missing one required table isn't "mostly
  right," so it's exact set equality instead. Every one of these is pure and unit-tested with
  hand-built `QueryPlan` pairs (tests/test_scoring.py), independent of the benchmark data or
  any live call.
- `execution_accuracy` does not compare against an independently hand-written reference SQL
  string. It compiles the **ground-truth `QueryPlan`** through the exact same deterministic
  compiler the actual answer went through, executes both, and compares result ROWS
  (`scoring.rows_match`, order-independent). This is what the metric is actually supposed to
  measure per ARCHITECTURE.md's original spec ("different SQL text can produce the same
  correct result") — it needs an independently *correct ground-truth plan* (established by a
  human when writing the benchmark), not an independently-styled SQL string, since the
  compiler translating either plan is the same trusted, already-unit-tested code either way.
- `unsafe_query_blocking_rate` in this project's eval measures the **Intent Router's**
  ability to refuse to engage with unsafe-sounding requests (delete/drop/update requests,
  prompt injection, off-topic). It does not measure the SQL Safety Layer, because the IR
  structurally cannot express a mutation in the first place (`QueryPlan` has no DDL/DML
  concept at all) — the guard's AST-level mutation denylist is separate, already-tested
  defense-in-depth (`tests/test_guard.py`) for a different failure mode (a future bug in the
  compiler itself), not something this benchmark category is positioned to exercise.

**Current focus: Phase 10 (final polish) and, only if resumed, live verification of Phases 5-9
against a real API key / database / Slack workspace.** No new IR fields, no new algorithms, no
scope changes to the deterministic core going forward without an explicit new decision.
