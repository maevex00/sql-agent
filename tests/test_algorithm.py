from joinplanner.algorithm import build_symmetric_graph, find_join_path_to_target

# a -> b -> c chain, declared in one direction only (mirrors a real FK schema
# where nextarc only ever states "child references parent")
NEXTARC = {
    "a": [{"table_name": "b", "next_key": "b_id", "key": "b_id"}],
    "b": [{"table_name": "c", "next_key": "c_id", "key": "c_id"}],
}


def test_forward_direction_path_matches_declared_edges():
    graph = build_symmetric_graph(NEXTARC)
    path = find_join_path_to_target("c", graph, ["a"])
    assert [list(edge.values())[0]["table_name"] for edge in path] == ["b", "c"]


def test_reverse_direction_path_is_also_found():
    """This is the bug fix over the original prototype: BFS must be able to
    start from the table that only appears on the *referenced* side of a FK
    (here, 'c') and still reach a table that only appears on the *referencing*
    side ('a'), even though nextarc never declares a c -> b or b -> a edge.
    """
    graph = build_symmetric_graph(NEXTARC)
    path = find_join_path_to_target("a", graph, ["c"])
    assert [list(edge.values())[0]["table_name"] for edge in path] == ["b", "a"]


def test_reverse_edge_preserves_correct_join_key_direction():
    graph = build_symmetric_graph(NEXTARC)
    path = find_join_path_to_target("a", graph, ["c"])
    # c -> b edge: original was b.c_id = c.c_id, so from c's side it must
    # still read as (c's key) = (b's next_key), not swapped incorrectly.
    first_edge = path[0]["c"]
    assert first_edge == {"table_name": "b", "next_key": "c_id", "key": "c_id"}


def test_already_connected_returns_empty_path():
    graph = build_symmetric_graph(NEXTARC)
    assert find_join_path_to_target("a", graph, ["a", "b"]) == []


def test_unreachable_target_returns_empty_list():
    nextarc = {"a": [{"table_name": "b", "next_key": "id", "key": "id"}]}
    graph = build_symmetric_graph(nextarc)
    assert find_join_path_to_target("z", graph, ["a"]) == []


def test_bfs_finds_shortest_of_two_paths():
    # a connects to d via a 1-hop path (a-d) and also via a 3-hop path
    # (a-b-c-d); BFS must prefer the 1-hop path.
    nextarc = {
        "a": [
            {"table_name": "b", "next_key": "id", "key": "id"},
            {"table_name": "d", "next_key": "id", "key": "id"},
        ],
        "b": [{"table_name": "c", "next_key": "id", "key": "id"}],
        "c": [{"table_name": "d", "next_key": "id", "key": "id"}],
    }
    graph = build_symmetric_graph(nextarc)
    path = find_join_path_to_target("d", graph, ["a"])
    assert len(path) == 1
    assert list(path[0].values())[0]["table_name"] == "d"
