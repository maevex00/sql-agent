import pytest

from safety.guard import check_read_only


def test_plain_select_passes():
    result = check_read_only("SELECT a, b FROM t WHERE a > 1 LIMIT 10")
    assert result.ok
    assert "LIMIT 10" in result.sql


def test_select_without_limit_gets_one_injected():
    result = check_read_only("SELECT a FROM t", max_limit=500)
    assert result.ok
    assert "LIMIT 500" in result.sql


def test_select_with_excessive_limit_gets_clamped():
    result = check_read_only("SELECT a FROM t LIMIT 999999", max_limit=1000)
    assert result.ok
    assert "LIMIT 1000" in result.sql


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (a) VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN x INT",
        "CREATE TABLE t2 (x INT)",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO someone",
    ],
)
def test_mutation_and_ddl_statements_are_blocked(sql):
    result = check_read_only(sql)
    assert not result.ok


def test_stacked_statements_are_blocked():
    # sqlglot parses a stacked statement as a Block root, outside the v1
    # {Select} allowlist -- rejected before the AST walk even needs to run.
    result = check_read_only("SELECT a FROM t; DROP TABLE t;")
    assert not result.ok


def test_select_with_subquery_still_passes():
    result = check_read_only("SELECT a FROM t WHERE a IN (SELECT a FROM t2)")
    assert result.ok


def test_cte_select_passes_v1_allowlist():
    result = check_read_only("WITH x AS (SELECT 1 AS a) SELECT a FROM x")
    assert result.ok


def test_unparseable_sql_is_rejected_not_raised():
    result = check_read_only("SELECT FROM WHERE ;;; garbage")
    assert not result.ok
    assert result.reason is not None
