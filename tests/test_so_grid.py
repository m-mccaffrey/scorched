import pytest

from standing_orders.grid import (COST_FOREST, COST_OPEN, MapError, TileMap,
                                  find_path, line_of_sight, visible_tiles)
from standing_orders.game import available_maps, load_map

SIMPLE = """!name Simple
!players 2
1.......
..#####.
........
.......2
"""


def test_parse_basics():
    m = TileMap.parse(SIMPLE)
    assert (m.width, m.height) == (8, 4)
    assert m.spawns == {1: (0, 0), 2: (7, 3)}
    assert m.info.name == "Simple"


def test_short_rows_are_padded_not_rejected():
    m = TileMap.parse("!players 2\n1..\n..2\n.\n")
    assert all(len(row) == m.width for row in m.tiles)


def test_metadata_and_comments():
    m = TileMap.parse("!name Fancy\n!players 2\n!teams yes\n; a comment\n1.\n.2\n")
    assert m.info.name == "Fancy" and m.info.teams is True


@pytest.mark.parametrize("text,why", [
    ("!players 2\n1.......\n", "at least two spawn"),
    ("!players 2\n1..3\n....\n", "numbered"),
    ("!players 2\n1..1\n....\n", "duplicate"),
    ("!players 2\n1..X\n...2\n", "unknown tile"),
    ("!players 2\n", "no map rows"),
])
def test_bad_maps_are_rejected(text, why):
    with pytest.raises(MapError) as caught:
        TileMap.parse(text)
    assert why in str(caught.value)


def test_map_too_large_is_rejected():
    with pytest.raises(MapError):
        TileMap.parse("!players 2\n" + "\n".join("1" + "." * 40 for _ in range(3)))


def test_wire_roundtrip():
    m = TileMap.parse(SIMPLE)
    assert TileMap.from_wire(m.to_wire()).tiles == m.tiles


def test_terrain_costs_and_passability():
    m = TileMap.parse("!players 2\n1.%#~$\n.....2\n")
    assert m.cost(1, 0) == COST_OPEN
    assert m.cost(2, 0) == COST_FOREST
    assert not m.passable(3, 0) and not m.passable(4, 0)
    assert m.passable(5, 0) and m.is_node(5, 0)


def test_path_routes_around_walls():
    m = TileMap.parse(SIMPLE)
    path = find_path(m, (0, 0), (7, 0))
    assert path and path[-1] == (7, 0)
    assert all(m.passable(*tile) for tile in path)


def test_path_is_four_directional():
    m = TileMap.parse("!players 2\n1..\n...\n..2\n")
    path = find_path(m, (0, 0), (2, 2))
    previous = (0, 0)
    for tile in path:
        assert abs(tile[0] - previous[0]) + abs(tile[1] - previous[1]) == 1
        previous = tile


def test_path_blocked_by_occupants():
    m = TileMap.parse("!players 2\n1#.\n.#.\n.#2\n")
    assert find_path(m, (0, 0), (2, 2)) == []


def test_no_path_to_impassable_goal():
    m = TileMap.parse("!players 2\n1#.\n...\n..2\n")
    assert find_path(m, (0, 0), (1, 0)) == []


def test_forest_blocks_sight_but_not_movement():
    m = TileMap.parse("!players 2\n1%..\n...2\n")
    assert find_path(m, (0, 0), (3, 0)) != []
    assert not line_of_sight(m, (0, 0), (3, 0))
    assert line_of_sight(m, (0, 0), (1, 0))      # you see the wood itself


def test_vision_is_bounded_by_radius():
    rows = ["1" + "." * 11] + ["." * 12 for _ in range(7)] + ["." * 11 + "2"]
    m = TileMap.parse("!players 2\n" + "\n".join(rows) + "\n")
    near = visible_tiles(m, (5, 4), 2)
    assert (5, 4) in near
    assert (11, 8) not in near
    assert all((x - 5) ** 2 + (y - 4) ** 2 <= 4 for x, y in near)


def test_shipped_maps_are_playable():
    """Every spawn must reach every other spawn, and every node must be
    reachable -- a sealed-off resource is dead content."""
    for name in available_maps():
        m = load_map(name)
        slots = sorted(m.spawns)
        for i in slots:
            for j in slots:
                if i < j:
                    assert find_path(m, m.spawns[i], m.spawns[j]), \
                        f"{name}: spawn {i} cannot reach spawn {j}"
        for node in m.nodes:
            assert find_path(m, m.spawns[1], node), \
                f"{name}: node {node} is unreachable"


def test_shipped_maps_declare_enough_spawns():
    for name in available_maps():
        m = load_map(name)
        assert len(m.spawns) >= m.info.players
        if m.info.teams:
            # 2v2 pairs spawns 1&3 against 2&4, so a team map needs all four.
            assert len(m.spawns) >= 4
