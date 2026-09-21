from pathlib import Path
from types import SimpleNamespace

import pytest

from joinplanner.algorithm import build_symmetric_graph
from joinplanner.sql_builder import (
    SchemaCatalog,
    build_join_plan,
    plan_join_for_fields,
    select_covering_tables,
)

CORRELATION_PATH = Path(__file__).parent.parent / "schema" / "correlation.json"


@pytest.fixture(scope="module")
def schema() -> SchemaCatalog:
    return SchemaCatalog(CORRELATION_PATH)


def test_field_with_single_owner_table_is_direct(schema):
    tables = select_covering_tables(schema, ["product_name"])
    assert tables == ["products"]


def test_unambiguous_fields_use_declared_order_no_greedy(schema):
    # product_name and category_name each have exactly one owner table --
    # nothing ambiguous, so both are taken directly with no set-cover pass.
    tables = select_covering_tables(schema, ["product_name", "category_name"])
    assert set(tables) == {"products", "categories"}


def test_ambiguous_field_with_only_two_candidates_still_triggers_greedy(schema):
    # unit_cost is products-only; category_id is owned by BOTH categories and
    # products (categories has its own PK category_id, products has the FK).
    # Only 2 candidate tables total, but category_id is still ambiguous, so
    # greedy must run -- and it should prefer the single table (products)
    # that covers both fields over splitting across two tables.
    tables = select_covering_tables(schema, ["unit_cost", "category_id"])
    assert tables == ["products"]


def test_greedy_prefers_table_covering_multiple_fields(schema):
    # region_id is owned by {warehouses, sales_reps, customers, sales_targets,
    # regions} (5 candidates -> greedy path). customer_name only lives on
    # customers. Greedy should pick 'customers' for region_id too, since that
    # covers both fields with one table instead of two.
    tables = select_covering_tables(schema, ["region_id", "customer_name"])
    assert tables == ["customers"]


def test_unknown_field_raises():
    schema = SchemaCatalog(CORRELATION_PATH)
    with pytest.raises(ValueError, match="not found in schema catalog"):
        select_covering_tables(schema, ["not_a_real_field"])


def test_join_plan_full_six_table_chain(schema):
    # regions -> customers -> orders -> order_items -> products -> categories,
    # exactly the multi-hop chain the schema was designed to require.
    fields = ["region_name", "customer_name", "order_date", "quantity", "product_name", "category_name"]
    join_plan, field_table = plan_join_for_fields(schema, fields)

    joined_tables = {step.table for step in join_plan.steps}
    assert joined_tables == {"regions", "customers", "orders", "order_items", "products", "categories"}

    # every step after the first (FROM) must be a LEFT JOIN with valid keys
    assert join_plan.steps[0].join_type == "FROM"
    for step in join_plan.steps[1:]:
        assert step.join_type == "LEFT JOIN"
        assert step.on_left_alias in join_plan.table_alias.values()
        assert step.on_left_key and step.on_right_key

    assert field_table["region_name"] == "regions"
    assert field_table["product_name"] == "products"


def test_join_plan_picks_shorter_path_warehouses_to_products(schema):
    # warehouses -> products has a unique shortest path via inventory (2
    # hops); the only alternative route (via regions/customers/.../order_items)
    # is much longer. BFS must take the 2-hop path, not the long way round.
    fields = ["warehouse_name", "product_name"]
    join_plan, _ = plan_join_for_fields(schema, fields)
    joined_tables = {step.table for step in join_plan.steps}
    assert joined_tables == {"warehouses", "inventory", "products"}


def test_build_join_plan_raises_on_unreachable_table():
    # Real correlation.json is one connected component by design, so this
    # uses a small hand-rolled disconnected graph instead: 'a' and 'z' share
    # no edges at all.
    nextarc = {"a": [{"table_name": "b", "next_key": "id", "key": "id"}]}
    fake_schema = SimpleNamespace(tables=["a", "b", "z"], graph=build_symmetric_graph(nextarc))
    with pytest.raises(ValueError, match="No JOIN path found"):
        build_join_plan(fake_schema, ["a", "z"])
