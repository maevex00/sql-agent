"""Generate synthetic data for the analytics schema (schema/ddl_postgres.sql).

Connects with the admin credential (seeding requires INSERT, which the app's
read-only role deliberately cannot do). Run via `make seed` after `docker
compose up`.
"""
from __future__ import annotations

import os
import random
from datetime import date, timedelta

import psycopg
from faker import Faker

fake = Faker()
random.seed(42)
Faker.seed(42)

ADMIN_DATABASE_URL = os.environ.get(
    "ADMIN_DATABASE_URL",
    "postgresql://app_admin:local_dev_only@localhost:5432/analytics",
)

N_REGIONS = 5
N_CATEGORIES = 8
N_SUPPLIERS = 10
N_PROMOTIONS = 6
N_WAREHOUSES = 10
N_SALES_REPS = 20
N_CUSTOMERS = 200
N_PRODUCTS = 150
N_ORDERS = 800
N_REVIEWS = 400
N_ORDER_ITEMS = 2000
TARGET_MONTHS_BACK = 12


def random_date(days_back: int) -> date:
    return date.today() - timedelta(days=random.randint(0, days_back))


def seed(conn: psycopg.Connection) -> None:
    cur = conn.cursor()

    region_ids = [
        cur.execute(
            "INSERT INTO regions (region_name) VALUES (%s) RETURNING region_id",
            (f"{fake.unique.state()} Region",),
        ).fetchone()[0]
        for _ in range(N_REGIONS)
    ]

    category_ids = [
        cur.execute(
            "INSERT INTO categories (category_name) VALUES (%s) RETURNING category_id",
            (fake.unique.word().capitalize() + " Goods",),
        ).fetchone()[0]
        for _ in range(N_CATEGORIES)
    ]

    supplier_ids = [
        cur.execute(
            "INSERT INTO suppliers (supplier_name, country) VALUES (%s, %s) RETURNING supplier_id",
            (fake.unique.company(), fake.country()),
        ).fetchone()[0]
        for _ in range(N_SUPPLIERS)
    ]

    promotion_ids = [
        cur.execute(
            "INSERT INTO promotions (promotion_name, discount_pct) VALUES (%s, %s) RETURNING promotion_id",
            (fake.unique.bs().capitalize(), round(random.uniform(5, 30), 2)),
        ).fetchone()[0]
        for _ in range(N_PROMOTIONS)
    ]

    warehouse_ids = [
        cur.execute(
            "INSERT INTO warehouses (warehouse_name, region_id) VALUES (%s, %s) RETURNING warehouse_id",
            (f"{fake.city()} DC", random.choice(region_ids)),
        ).fetchone()[0]
        for _ in range(N_WAREHOUSES)
    ]

    sales_rep_ids = [
        cur.execute(
            "INSERT INTO sales_reps (rep_name, region_id) VALUES (%s, %s) RETURNING rep_id",
            (fake.name(), random.choice(region_ids)),
        ).fetchone()[0]
        for _ in range(N_SALES_REPS)
    ]

    customer_ids = [
        cur.execute(
            "INSERT INTO customers (customer_name, region_id, signup_date) VALUES (%s, %s, %s) RETURNING customer_id",
            (fake.name(), random.choice(region_ids), random_date(900)),
        ).fetchone()[0]
        for _ in range(N_CUSTOMERS)
    ]

    product_ids = [
        cur.execute(
            "INSERT INTO products (product_name, category_id, supplier_id, unit_cost) "
            "VALUES (%s, %s, %s, %s) RETURNING product_id",
            (
                fake.unique.catch_phrase(),
                random.choice(category_ids),
                random.choice(supplier_ids),
                round(random.uniform(5, 500), 2),
            ),
        ).fetchone()[0]
        for _ in range(N_PRODUCTS)
    ]

    for product_id in product_ids:
        for warehouse_id in random.sample(warehouse_ids, k=random.randint(1, 4)):
            cur.execute(
                "INSERT INTO inventory (product_id, warehouse_id, stock_qty) VALUES (%s, %s, %s)",
                (product_id, warehouse_id, random.randint(0, 500)),
            )

    for region_id in region_ids:
        for months_ago in range(TARGET_MONTHS_BACK):
            period = date.today().replace(day=1) - timedelta(days=30 * months_ago)
            cur.execute(
                "INSERT INTO sales_targets (region_id, period, target_amount) VALUES (%s, %s, %s)",
                (region_id, period.replace(day=1), round(random.uniform(20000, 80000), 2)),
            )

    order_ids = [
        cur.execute(
            "INSERT INTO orders (customer_id, rep_id, order_date, status) VALUES (%s, %s, %s, %s) RETURNING order_id",
            (
                random.choice(customer_ids),
                random.choice(sales_rep_ids),
                random_date(365),
                random.choice(["completed", "completed", "completed", "cancelled", "pending"]),
            ),
        ).fetchone()[0]
        for _ in range(N_ORDERS)
    ]

    for _ in range(N_REVIEWS):
        cur.execute(
            "INSERT INTO reviews (product_id, customer_id, rating, review_date) VALUES (%s, %s, %s, %s)",
            (
                random.choice(product_ids),
                random.choice(customer_ids),
                random.randint(1, 5),
                random_date(365),
            ),
        )

    for _ in range(N_ORDER_ITEMS):
        promotion_id = random.choice(promotion_ids + [None, None, None])  # ~25% promoted
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, promotion_id, quantity, unit_price) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                random.choice(order_ids),
                random.choice(product_ids),
                promotion_id,
                random.randint(1, 10),
                round(random.uniform(5, 500), 2),
            ),
        )

    conn.commit()
    print(
        f"Seeded {len(region_ids)} regions, {len(customer_ids)} customers, "
        f"{len(product_ids)} products, {len(order_ids)} orders, {N_ORDER_ITEMS} order_items."
    )


if __name__ == "__main__":
    with psycopg.connect(ADMIN_DATABASE_URL) as conn:
        seed(conn)
