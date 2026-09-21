-- Schema mirrors schema/correlation.json exactly. Any table/column/relationship
-- change must be made in both places.

CREATE TABLE regions (
    region_id   SERIAL PRIMARY KEY,
    region_name TEXT NOT NULL
);

CREATE TABLE categories (
    category_id   SERIAL PRIMARY KEY,
    category_name TEXT NOT NULL
);

CREATE TABLE suppliers (
    supplier_id   SERIAL PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    country       TEXT
);

CREATE TABLE promotions (
    promotion_id   SERIAL PRIMARY KEY,
    promotion_name TEXT NOT NULL,
    discount_pct   NUMERIC(5, 2) NOT NULL
);

CREATE TABLE warehouses (
    warehouse_id   SERIAL PRIMARY KEY,
    warehouse_name TEXT NOT NULL,
    region_id      INT NOT NULL REFERENCES regions(region_id)
);

CREATE TABLE sales_reps (
    rep_id    SERIAL PRIMARY KEY,
    rep_name  TEXT NOT NULL,
    region_id INT NOT NULL REFERENCES regions(region_id)
);

CREATE TABLE customers (
    customer_id   SERIAL PRIMARY KEY,
    customer_name TEXT NOT NULL,
    region_id     INT NOT NULL REFERENCES regions(region_id),
    signup_date   DATE NOT NULL
);

CREATE TABLE products (
    product_id   SERIAL PRIMARY KEY,
    product_name TEXT NOT NULL,
    category_id  INT NOT NULL REFERENCES categories(category_id),
    supplier_id  INT NOT NULL REFERENCES suppliers(supplier_id),
    unit_cost    NUMERIC(10, 2) NOT NULL
);

CREATE TABLE inventory (
    inventory_id SERIAL PRIMARY KEY,
    product_id   INT NOT NULL REFERENCES products(product_id),
    warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    stock_qty    INT NOT NULL
);

CREATE TABLE sales_targets (
    target_id     SERIAL PRIMARY KEY,
    region_id     INT NOT NULL REFERENCES regions(region_id),
    period        DATE NOT NULL,       -- first day of the target month
    target_amount NUMERIC(12, 2) NOT NULL
);

CREATE TABLE orders (
    order_id    SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(customer_id),
    rep_id      INT NOT NULL REFERENCES sales_reps(rep_id),
    order_date  DATE NOT NULL,
    status      TEXT NOT NULL
);

CREATE TABLE reviews (
    review_id    SERIAL PRIMARY KEY,
    product_id   INT NOT NULL REFERENCES products(product_id),
    customer_id  INT NOT NULL REFERENCES customers(customer_id),
    rating       INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_date  DATE NOT NULL
);

CREATE TABLE order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INT NOT NULL REFERENCES orders(order_id),
    product_id    INT NOT NULL REFERENCES products(product_id),
    promotion_id  INT REFERENCES promotions(promotion_id),
    quantity      INT NOT NULL,
    unit_price    NUMERIC(10, 2) NOT NULL
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_rep ON orders(rep_id);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_inventory_product ON inventory(product_id);
CREATE INDEX idx_inventory_warehouse ON inventory(warehouse_id);
CREATE INDEX idx_reviews_product ON reviews(product_id);

-- Second line of defense for the SQL Safety Layer: the application never
-- connects with the admin role. This role can SELECT and nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'readonly_agent') THEN
        CREATE ROLE readonly_agent LOGIN PASSWORD 'readonly_dev_only';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE analytics TO readonly_agent;
GRANT USAGE ON SCHEMA public TO readonly_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly_agent;
ALTER ROLE readonly_agent SET statement_timeout = '5s';
