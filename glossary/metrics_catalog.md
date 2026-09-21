# Standard Metric Definitions

Canonical `(field, func)` pairs for common analytics questions, so the LLM Query Planner
doesn't have to re-derive the "obvious" choice every time and doesn't drift between
sessions. Each entry names the aggregation and the field it applies to; the LLM still
chooses dimensions, filters, and time range from the question.

| Question pattern | Metric |
|---|---|
| "how many units / how much volume" | `sum(quantity)` |
| "average order size" | `avg(quantity)` |
| "how many orders" | `count_distinct(order_id)` |
| "how many customers" | `count_distinct(customer_id)` |
| "total revenue" / "total sales" (see glossary/terms.md's known gap) | `sum(unit_price)` |
| "average price" | `avg(unit_price)` |
| "average rating" / "customer satisfaction" | `avg(rating)` |
| "total stock" / "inventory on hand" | `sum(stock_qty)` |
| "sales target" / "quota" | `sum(target_amount)` |
| "which X missed / beat their target" | `metric_comparisons`: `sum(unit_price) < sum(target_amount)` (missed) or `>` (beat) |

## Time range defaults

If the user gives no explicit period, do not silently default to all-time for a
time-series-shaped question (e.g. "sales trend", "this month's orders") -- ask has no
clarification turn in v1, so prefer the most recently completed standard period implied by
the phrasing: "this X" -> `this_X` (to-date), "last X" -> `last_X` (full prior period), no
qualifier on an otherwise time-shaped question -> `last_month`.
