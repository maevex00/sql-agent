"""Graph JOIN planning: symmetric BFS shortest path over the schema relationship graph.

Topological sort was intentionally removed -- see ARCHITECTURE.md, "Graph JOIN
Planner". The only property the planner ever needed from table ordering was a
fixed, deterministic iteration order, which `correlation.json`'s declared
`tables` list already provides without the crash-on-cycle risk a topological
sort carries on a real (not-necessarily-acyclic) FK graph.
"""
from __future__ import annotations

from collections import deque
from typing import Any

Edge = dict[str, str]
RelationGraph = dict[str, list[Edge]]


def build_symmetric_graph(nextarc: dict[str, list[dict[str, Any]]]) -> RelationGraph:
    """Make the declared FK relation graph traversable in both directions.

    `nextarc` only declares an edge in the direction the FK happens to point
    (e.g. "order_items" -> "orders", because order_items.order_id references
    orders.order_id). BFS starting from an anchor table on the "referenced"
    side (e.g. "orders") would never find a path to a "referencing" table
    unless the reverse edge also exists. Every edge is therefore inserted in
    both directions, with `key`/`next_key` swapped on the reverse edge so the
    compiled JOIN condition (`old.key = new.next_key`) stays correct regardless
    of which side ends up as the "old" (already-connected) table.
    """
    graph: RelationGraph = {}
    for src, edges in nextarc.items():
        graph.setdefault(src, [])
        for edge in edges:
            dst = edge["table_name"]
            graph[src].append({"table_name": dst, "key": edge["key"], "next_key": edge["next_key"]})
            graph.setdefault(dst, [])
            graph[dst].append({"table_name": src, "key": edge["next_key"], "next_key": edge["key"]})
    return graph


def find_join_path_to_target(
    target_table: str,
    graph: RelationGraph,
    connected_tables: list[str],
) -> list[dict[str, dict[str, str]]]:
    """BFS shortest path from the already-connected table set to target_table.

    :param graph: symmetric relation graph, see build_symmetric_graph()
    :param connected_tables: multi-source BFS frontier (tables already joined)
    :return: ordered list of {"<from_table>": {"table_name": <to_table>,
             "next_key": ..., "key": ...}} edges to walk, root to target.
             Empty list if target is already connected or unreachable.
    """
    if target_table in connected_tables:
        return []

    queue = deque(connected_tables)
    visited = set(connected_tables)
    parent: dict[str, tuple[str, dict]] = {}

    while queue:
        current = queue.popleft()
        if current == target_table:
            path = []
            node = current
            while node in parent:
                parent_node, edge_info = parent[node]
                path.append(edge_info)
                node = parent_node
            path.reverse()
            return path

        for rel in graph.get(current, []):
            neighbor = rel["table_name"]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
                edge_info = {
                    current: {
                        "table_name": neighbor,
                        "next_key": rel["next_key"],
                        "key": rel["key"],
                    }
                }
                parent[neighbor] = (current, edge_info)

    return []
