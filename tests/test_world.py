import math

import pytest

from aiverse_server.layout import MAP_SIZE, create_default_world
from aiverse_server.world import Position, Resident, World, WorldMap


def resident(id: str, x: float, y: float) -> Resident:
    return Resident(id=id, name=id, color="#ffffff", position=Position(x, y))


def test_default_world_is_a_square_map_with_residents_inside() -> None:
    world = create_default_world()
    assert world.map.size == MAP_SIZE
    assert len(world.residents) == 4
    assert all(world.map.contains(r.position) for r in world.residents)


@pytest.mark.parametrize(("x", "y"), [(0, 0), (10, 0), (0, 10), (10, 10), (3.25, 7.5)])
def test_edges_and_fractional_positions_are_inside(x: float, y: float) -> None:
    assert WorldMap(size=10).contains(Position(x, y))


@pytest.mark.parametrize(
    ("x", "y"), [(-0.01, 5), (5, -0.01), (10.01, 5), (5, 10.01), (math.nan, 5), (5, math.inf)]
)
def test_positions_beyond_the_edges_are_outside(x: float, y: float) -> None:
    assert not WorldMap(size=10).contains(Position(x, y))


@pytest.mark.parametrize("size", [0, -1, math.nan, math.inf])
def test_map_size_must_be_positive_and_finite(size: float) -> None:
    with pytest.raises(ValueError):
        WorldMap(size=size)


def test_world_rejects_residents_outside_the_map() -> None:
    with pytest.raises(ValueError, match="outside the map"):
        World(map=WorldMap(size=10), residents=(resident("a", 11, 5),))


@pytest.mark.parametrize("color", ["red", "#fff", "#12345g", "#1234567", "112233"])
def test_resident_color_must_be_hex_rrggbb(color: str) -> None:
    with pytest.raises(ValueError, match="invalid color"):
        Resident(id="a", name="a", color=color, position=Position(1, 1))


def test_world_rejects_duplicate_resident_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        World(map=WorldMap(size=10), residents=(resident("a", 1, 1), resident("a", 2, 2)))
