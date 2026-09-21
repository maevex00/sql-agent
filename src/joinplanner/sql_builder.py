"""Anchor/table-cover selection (conditional greedy) + JOIN skeleton assembly.

See ARCHITECTURE.md, "Graph JOIN Planner": the greedy set-cover step only runs
when at least one requested field is owned by more than one table (the same
column name -- most often a foreign key -- can legitimately live on more than
one table, e.g. `region_id` on both `customers` and `regions`). When no field
is ambiguous there is nothing to optimize, so BFS alone determines the join.
"""
from __future__ import annotations

import json
from pathlib import Path

from joinplanner.algorithm import build_symmetric_graph, find_join_path_to_target
from joinplanner.models import JoinPlan, JoinStep


class SchemaCatalog:
    """Loads and indexes schema/correlation.json."""

    def __init__(self, correlation_path: str | Path):
        data = json.loads(Path(correlation_path).read_text(encoding="utf-8"))
        self.tables: list[str] = data["tables"]
        self.table_column: dict[str, list[str]] = data["table_column"]
        self.graph = build_symmetric_graph(data["nextarc"])
        self._field_to_tables: dict[str, list[str]] = {}
        for table in self.tables:
            for f in self.table_column.get(table, []):
                self._field_to_tables.setdefault(f, []).append(table)

    def tables_for_field(self, field: str) -> list[str]:
        return self._field_to_tables.get(field, [])

    def all_fields(self) -> list[str]:
        """Every known field name across every table, for the LLM's field catalog
        prompt and the Schema Resolver's fuzzy-match candidate pool."""
        return list(self._field_to_tables.keys())

    def fields_of(self, table: str) -> list[str]:
        return self.table_column.get(table, [])


def select_covering_tables(schema: SchemaCatalog, fields: list[str]) -> list[str]:
    """Pick a set of tables that together supply every requested field.

    - Validates every field resolves to at least one table (raises otherwise --
      this is exactly the failure the Schema Resolver's repair path handles
      upstream in the full pipeline; here it is a hard error).
    - The trigger for running greedy optimization is *field-level ambiguity*
      (some requested field is owned by 2+ tables -- most often a shared FK
      column, e.g. `region_id` living on both `customers` and `regions`), not
      a raw candidate-table count. A candidate-table-count threshold turned
      out to hide real ambiguity: e.g. `unit_cost` (products only) plus
      `category_id` (categories or products) has only 2 candidate tables, but
      still has a genuine "1 table or 2?" choice to make -- caught by
      tests/test_sql_builder.py and tests/test_compiler.py during
      development, see ARCHITECTURE.md changelog note.
    - No field is ambiguous -> nothing to optimize; each field maps directly
      to its single owner, in schema-declared order for determinism.
    - Any field is ambiguous -> greedy max-coverage set cover over the union
      of all candidate tables, iterating in schema-declared order.
    """
    unresolved = list(dict.fromkeys(fields))  # de-dup, preserve order

    missing = [f for f in unresolved if not schema.tables_for_field(f)]
    if missing:
        raise ValueError(f"Fields not found in schema catalog: {missing}")

    has_ambiguous_field = any(len(schema.tables_for_field(f)) > 1 for f in unresolved)

    if not has_ambiguous_field:
        chosen: list[str] = []
        for f in unresolved:
            owner = schema.tables_for_field(f)[0]
            if owner not in chosen:
                chosen.append(owner)
        return chosen

    candidate_tables: list[str] = []
    for f in unresolved:
        for t in schema.tables_for_field(f):
            if t not in candidate_tables:
                candidate_tables.append(t)

    remaining = set(unresolved)
    chosen = []
    pool = [t for t in schema.tables if t in candidate_tables]
    while remaining:
        best_table, best_cover = None, set()
        for t in pool:
            cover = remaining & set(schema.fields_of(t))
            if len(cover) > len(best_cover):
                best_table, best_cover = t, cover
        if best_table is None:
            raise ValueError(f"Cannot cover remaining fields: {remaining}")
        chosen.append(best_table)
        remaining -= best_cover
        pool.remove(best_table)
    return chosen


def build_join_plan(schema: SchemaCatalog, tables_needed: list[str]) -> JoinPlan:
    """Connect `tables_needed` into one JOIN skeleton via BFS shortest paths.

    Anchor table = first entry in schema-declared order among `tables_needed`
    (deterministic tie-break; see ARCHITECTURE.md).

    Remaining targets are connected **nearest-first**, not in schema-declared
    order: at each step, BFS is run from the current connected set to every
    still-unconnected required table, and whichever is currently *closest* is
    connected next (ties broken by schema-declared order). This is
    recomputed after every connection because connecting one required table
    can shorten -- often to zero -- the distance to another.

    This matters because processing targets in a fixed arbitrary order can
    route an early target through an irrelevant "shortcut" table that a later
    target would have made redundant. Concretely, in the demo schema,
    connecting `categories` before `order_items` was found to route through
    `reviews` (a genuine shorter path from `customers` to `products` that has
    nothing to do with the query), because at that point `order_items` -- the
    table that would have provided a direct, on-topic path -- hadn't been
    connected yet. Nearest-first avoids this: `orders`/`order_items` end up
    connected before `categories` is even considered, so `categories`
    connects via `products` (1 hop) instead of via `reviews` (a 4-hop
    detour). Caught by tests/test_sql_builder.py during development.
    """
    if not tables_needed:
        raise ValueError("tables_needed must be non-empty")

    ordered_targets = [t for t in schema.tables if t in tables_needed]
    anchor, *targets = ordered_targets
    remaining = set(targets)

    alias_of: dict[str, str] = {anchor: "T0"}
    steps = [JoinStep(table=anchor, alias="T0", join_type="FROM")]
    counter = 1

    while remaining:
        remaining -= alias_of.keys()  # targets connected as a side effect of a prior hop
        if not remaining:
            break

        connected_now = list(alias_of.keys())
        best_target, best_path = None, None
        for t in sorted(remaining, key=schema.tables.index):
            path = find_join_path_to_target(t, schema.graph, connected_now)
            if not path:
                continue  # unreachable from the current connected set -- not a candidate
            if best_path is None or len(path) < len(best_path):
                best_target, best_path = t, path

        if best_target is None:
            raise ValueError(
                f"No JOIN path found from {connected_now} to any of {sorted(remaining)} -- "
                "schema graph does not connect these tables"
            )

        for edge in best_path:
            (from_table, edge_info), = edge.items()
            to_table = edge_info["table_name"]
            if to_table not in alias_of:
                alias = f"T{counter}"
                counter += 1
                alias_of[to_table] = alias
                steps.append(
                    JoinStep(
                        table=to_table,
                        alias=alias,
                        join_type="LEFT JOIN",
                        on_left_alias=alias_of[from_table],
                        on_left_key=edge_info["key"],
                        on_right_key=edge_info["next_key"],
                    )
                )
        remaining.discard(best_target)

    return JoinPlan(steps=steps, table_alias=alias_of)


def plan_join_for_fields(schema: SchemaCatalog, fields: list[str]) -> tuple[JoinPlan, dict[str, str]]:
    """End-to-end: requested fields -> covering tables -> JoinPlan + field->table map."""
    tables_needed = select_covering_tables(schema, fields)
    join_plan = build_join_plan(schema, tables_needed)

    field_table: dict[str, str] = {}
    for f in fields:
        owners = schema.tables_for_field(f)
        owned_by_chosen = [t for t in owners if t in tables_needed]
        field_table[f] = owned_by_chosen[0] if owned_by_chosen else owners[0]

    return join_plan, field_table
