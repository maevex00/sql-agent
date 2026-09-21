# Business Terminology

Plain-language terms mapped to the schema field they refer to. This file is injected into
the LLM Query Planner's prompt so it can translate a user's wording into the field catalog
without guessing. It is deliberately generic (retail/e-commerce demo domain) -- see
ARCHITECTURE.md's design principle: the LLM understands intent, but even *which field a
word refers to* should be looked up here rather than inferred fresh every time.

| Term (as a user might say it) | Field | Notes |
|---|---|---|
| region, territory | `region_name` | Always the name, not `region_id`, unless the user explicitly asks for an ID. |
| customer, client, buyer | `customer_name` | |
| product, item, SKU | `product_name` | |
| category, product line | `category_name` | |
| supplier, vendor | `supplier_name` | |
| sales rep, account owner | `rep_name` | |
| warehouse, distribution center, DC | `warehouse_name` | |
| units sold, quantity sold, volume | `quantity` (on `order_items`) | |
| price, unit price, selling price | `unit_price` (on `order_items`) | Per line item, not the catalog list price. |
| cost, unit cost, COGS | `unit_cost` (on `products`) | |
| stock level, inventory, on-hand | `stock_qty` (on `inventory`) | |
| discount, promo rate | `discount_pct` (on `promotions`) | |
| rating, customer satisfaction, review score | `rating` (on `reviews`) | 1-5 scale. |
| order status, fulfillment status | `status` (on `orders`) | Values: `completed`, `cancelled`, `pending`. |
| sales target, quota, goal | `target_amount` (on `sales_targets`) | Monthly, one row per region per month. |
| signup date, customer since | `signup_date` (on `customers`) | |
| order date, purchase date | `order_date` (on `orders`) | |

## Known gap: no precomputed revenue/line-total column

This demo schema does **not** have a `revenue` or `line_total` column -- `order_items` has
`unit_price` and `quantity` as separate columns, and the current `QueryPlan` IR can only
aggregate a single column per metric (no `price * quantity` expression). If a user asks for
"revenue" or "total sales", the closest available metric is `SUM(unit_price)`, which is
summed line-item prices, **not** price-times-quantity. This is a known simplification of the
demo schema, not a bug -- flagging it here so the LLM (and anyone reading this file) doesn't
overclaim what the number means. See ARCHITECTURE.md if this needs to become a real metric
later (it would need a derived/computed-column concept the IR does not have yet).
