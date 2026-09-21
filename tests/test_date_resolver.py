from datetime import date

from planner.date_resolver import resolve
from planner.query_plan import AbsoluteTimeRange, RelativeTimeRange

# Fixed reference date so every test has exactly one correct answer,
# independent of when the suite is actually run: Wednesday 2026-09-23.
NOW = date(2026, 9, 23)


def _resolve(period: str, n: int | None = None) -> tuple[date, date]:
    rt = RelativeTimeRange(field="order_date", period=period, n=n)
    result = resolve(rt, NOW)
    return result.start, result.end


def test_absolute_passthrough():
    absolute = AbsoluteTimeRange(field="order_date", start=date(2026, 1, 1), end=date(2026, 1, 31))
    assert resolve(absolute, NOW) is absolute


def test_last_n_days():
    start, end = _resolve("last_n_days", n=30)
    assert end == NOW
    assert start == date(2026, 8, 25)  # 30 days inclusive of NOW


def test_this_week_monday_to_today():
    start, end = _resolve("this_week")
    assert start == date(2026, 9, 21)  # Monday of that week
    assert end == NOW


def test_last_week_full_prior_week():
    start, end = _resolve("last_week")
    assert start == date(2026, 9, 14)
    assert end == date(2026, 9, 20)


def test_this_month_and_month_to_date_are_equivalent():
    a = _resolve("this_month")
    b = _resolve("month_to_date")
    assert a == b == (date(2026, 9, 1), NOW)


def test_last_month_full_prior_month():
    start, end = _resolve("last_month")
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 31)


def test_this_quarter_to_date():
    start, end = _resolve("this_quarter")
    assert start == date(2026, 7, 1)  # Q3 starts July
    assert end == NOW


def test_last_quarter_full_prior_quarter():
    start, end = _resolve("last_quarter")
    assert start == date(2026, 4, 1)
    assert end == date(2026, 6, 30)


def test_this_year_and_year_to_date():
    a = _resolve("this_year")
    b = _resolve("year_to_date")
    assert a == b == (date(2026, 1, 1), NOW)


def test_last_year_full_prior_year():
    start, end = _resolve("last_year")
    assert start == date(2025, 1, 1)
    assert end == date(2025, 12, 31)


def test_quarter_boundary_january():
    start, _ = _resolve("this_quarter", n=None)
    # sanity check for a different reference date crossing year boundary
    jan_now = date(2026, 1, 15)
    rt = RelativeTimeRange(field="order_date", period="last_quarter")
    result = resolve(rt, jan_now)
    assert result.start == date(2025, 10, 1)
    assert result.end == date(2025, 12, 31)
