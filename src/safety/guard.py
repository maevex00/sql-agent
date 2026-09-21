"""SQL Safety Layer: application-level guard. Second line of defense behind the
read-only DB role (schema/ddl_postgres.sql grants SELECT-only to `readonly_agent`).

General principle (not an MVP-only rule): a query is safe to execute iff its
AST contains no mutation/DDL node *anywhere in the tree* -- checked by walking
the whole AST, not by asserting the root node's type. A root-type whitelist
alone would incidentally reject legal read-only constructs.

v1 scope, kept explicitly separate from the rule above: `_ALLOWED_ROOT_TYPES`
is `{exp.Select}`. In this sqlglot version a `WITH ... SELECT` (CTE) already
parses with root type `Select` (the CTE is an arg on the Select node, not a
separate wrapper), so CTEs already pass today. Only `UNION` needs a future
widening to `{Select, Union}` when QueryPlan grows a construct that needs one
-- the mutation-denylist walk below already covers a Union's contents safely,
so that widening is a one-line change, not a redesign.

Multi-statement input (`SELECT ...; DROP TABLE ...;`) is *not* special-cased:
sqlglot.parse_one on stacked statements returns a `Block` root, which is
already outside `_ALLOWED_ROOT_TYPES` and gets rejected by the same check --
confirmed by tests/test_guard.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

_ALLOWED_ROOT_TYPES: tuple[type[exp.Expression], ...] = (exp.Select,)

_MUTATION_DENYLIST: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Grant,
    exp.Merge,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Cache,
    exp.Command,  # catch-all for anything sqlglot couldn't classify as a known safe node
)


@dataclass
class GuardResult:
    ok: bool
    sql: str
    reason: str | None = None


def enforce_limit(ast: exp.Expression, max_limit: int) -> exp.Expression:
    """Clamp (or add, if absent) LIMIT to <= max_limit, regardless of what the
    compiled query already requested. This runs independently of
    `QueryPlan.limit`'s own cap (planner/query_plan.py) -- two layers, same
    invariant, neither trusting the other.
    """
    existing = ast.args.get("limit")
    requested = None
    if existing is not None:
        try:
            requested = int(existing.expression.this)
        except (AttributeError, TypeError, ValueError):
            requested = None
    if existing is None or requested is None or requested > max_limit:
        ast.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
    return ast


def check_read_only(sql: str, *, dialect: str = "postgres", max_limit: int = 10_000) -> GuardResult:
    """Validate `sql` is a read-only, single, LIMIT-bounded statement.

    Returns a GuardResult; never raises on malformed/unsafe input -- callers
    (execution layer) branch on `.ok` rather than catching exceptions, so an
    unsafe query is a normal, loggable outcome, not a control-flow surprise.
    """
    try:
        ast = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError as e:
        return GuardResult(ok=False, sql=sql, reason=f"unparseable SQL: {e}")

    if not isinstance(ast, _ALLOWED_ROOT_TYPES):
        return GuardResult(
            ok=False,
            sql=sql,
            reason=(
                f"top-level statement type '{type(ast).__name__}' not in v1 allowlist "
                f"{[t.__name__ for t in _ALLOWED_ROOT_TYPES]}"
            ),
        )

    for node in ast.walk():
        if isinstance(node, _MUTATION_DENYLIST):
            return GuardResult(ok=False, sql=sql, reason=f"disallowed AST node: {type(node).__name__}")

    ast = enforce_limit(ast, max_limit)
    return GuardResult(ok=True, sql=ast.sql(dialect=dialect, pretty=True))
