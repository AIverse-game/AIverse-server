"""Islands and bridges: visitors keep to the islands of a map and cross between them by bridge."""

import math

import pytest

from aiverse_server.core.world import Position, WorldMap
from aiverse_server.economy.islands import bridges, on_island, walk_from

ISLANDS = WorldMap(size=150, block_cells=3, block_price_factor=5, lobby=True)
"""Five by five islands, each 26 wide in a block of 30, so 4 apart: island 0 covers 2..28."""
PLAIN = WorldMap(size=100)


def test_each_island_has_a_bridge_to_its_neighbour_to_the_east_and_south() -> None:
    found = bridges(ISLANDS)

    assert len(found) == 40
    assert found[0] == (Position(28, 15), Position(32, 15))
    assert found[1] == (Position(15, 28), Position(15, 32))


def test_a_narrow_last_column_of_islands_is_joined_at_the_middle_of_its_sides() -> None:
    # 160 is sixteen parcels, so the sixth block holds one parcel and its island is 6 wide.
    found = bridges(WorldMap(size=160, block_cells=3))

    assert len(found) == 60
    assert found[8] == (Position(148, 15), Position(152, 15))
    assert (Position(155, 28), Position(155, 32)) in found


def test_a_map_of_single_parcel_blocks_has_no_islands() -> None:
    assert bridges(PLAIN) == ()


def test_on_one_island_the_walk_is_straight() -> None:
    walk = walk_from(ISLANDS, Position(5, 5))

    assert walk.distance(Position(25, 20)) == pytest.approx(25)
    assert walk.legs(Position(25, 20)) == ((Position(5, 5), Position(25, 20)),)


def test_to_the_next_island_the_walk_goes_to_the_bridge_and_across() -> None:
    walk = walk_from(ISLANDS, Position(15, 5))

    legs = walk.legs(Position(45, 5))

    # Straight would be 30, but the bridge is at y = 15.
    to_bridge = math.hypot(13, 10)
    assert legs == (
        (Position(15, 5), Position(28, 15)),
        (Position(28, 15), Position(32, 15)),
        (Position(32, 15), Position(45, 5)),
    )
    assert walk.distance(Position(45, 5)) == pytest.approx(2 * to_bridge + 4)


def test_to_a_diagonal_island_the_walk_crosses_two_bridges_through_the_island_between() -> None:
    walk = walk_from(ISLANDS, Position(15, 15))

    distance = walk.distance(Position(45, 45))

    # Over to (28, 15), across, on to (45, 28), across, then straight down to (45, 45).
    assert distance == pytest.approx(13 + 4 + math.hypot(13, 13) + 4 + 13)
    assert distance > math.dist((15, 15), (45, 45))
    assert sum(math.dist((a.x, a.y), (b.x, b.y)) for a, b in walk.legs(Position(45, 45))) == (
        pytest.approx(distance)
    )


def test_without_islands_every_walk_is_straight() -> None:
    walk = walk_from(PLAIN, Position(10, 10))

    assert walk.distance(Position(90, 10)) == 80
    assert walk.legs(Position(90, 10)) == ((Position(10, 10), Position(90, 10)),)


def test_a_spot_off_the_island_is_moved_back_onto_it() -> None:
    home = Position(15, 15)

    assert on_island(ISLANDS, home, Position(40, 15)) == Position(28, 15)
    assert on_island(ISLANDS, home, Position(-5, 20)) == Position(2, 20)
    assert on_island(ISLANDS, home, Position(10, 10)) == Position(10, 10)
    assert on_island(PLAIN, home, Position(-5, 20)) == Position(-5, 20)
