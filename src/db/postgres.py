"""Postgres execution: run already-guarded SQL against the read-only role.

Not covered by the test suite -- requires a live Postgres connection
(`make setup`). This module does not re-validate the SQL it's given; it
only ever accepts the output of safety/guard.py's check_read_only(), never
raw user- or LLM-provided text. `timeout_ms` reinforces, client-side, the
statement_timeout the read-only role already carries
(schema/ddl_postgres.sql) -- belt and braces, not the primary enforcement
mechanism, which is the DB role itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def execute(sql: str, *, database_url: str | None = None, timeout_ms: int = 5000) -> QueryResult:
    import psycopg

    url = database_url or os.environ["DATABASE_URL"]
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        cur.execute(sql)
        columns = [desc.name for desc in cur.description]
        rows = cur.fetchall()
    return QueryResult(columns=columns, rows=rows)
