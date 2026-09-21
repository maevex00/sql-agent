"""Pure function: RelativeTimeRange + a reference date -> AbsoluteTimeRange.

The LLM only ever selects a `period` enum value (+ `n` for last_n_days) -- it
never computes a date. `resolve()` takes `now` as an explicit argument and
never reads the clock itself, which is what makes it a pure function: "last
quarter" as of a fixed reference date has exactly one correct answer, and that
answer cannot depend on LLM sampling or wall-clock skew between planning and
execution.

"this_X" / "X_to_date" period pairs (e.g. this_month / month_to_date) resolve
identically -- they are kept as separate enum values only because both
phrasings are common in how people ask ("sales this month" vs "MTD sales"),
and a closed enum is cheaper for the LLM to hit reliably than forcing it to
normalize vocabulary itself.
"""
from __future__ import annotations

from datetime import date, timedelta

from planner.query_plan import AbsoluteTimeRange, RelativeTimeRange, TimeRange


def _quarter_start(d: date) -> date:
    quarter_start_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, quarter_start_month, 1)


def resolve(time_range: TimeRange, now: date) -> AbsoluteTimeRange:
    if isinstance(time_range, AbsoluteTimeRange):
        return time_range

    rt: RelativeTimeRange = time_range
    period = rt.period

    if period == "last_n_days":
        n = rt.n or 30
        start, end = now - timedelta(days=n - 1), now

    elif period == "this_week":
        start, end = now - timedelta(days=now.weekday()), now

    elif period == "last_week":
        this_monday = now - timedelta(days=now.weekday())
        start = this_monday - timedelta(days=7)
        end = this_monday - timedelta(days=1)

    elif period in ("this_month", "month_to_date"):
        start, end = now.replace(day=1), now

    elif period == "last_month":
        end = now.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)

    elif period in ("this_quarter", "quarter_to_date"):
        start, end = _quarter_start(now), now

    elif period == "last_quarter":
        end = _quarter_start(now) - timedelta(days=1)
        start = _quarter_start(end)

    elif period in ("this_year", "year_to_date"):
        start, end = now.replace(month=1, day=1), now

    elif period == "last_year":
        start, end = date(now.year - 1, 1, 1), date(now.year - 1, 12, 31)

    else:
        raise ValueError(f"Unhandled relative period: {period}")

    return AbsoluteTimeRange(field=rt.field, start=start, end=end)
