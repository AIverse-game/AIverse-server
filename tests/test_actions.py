import math

import pytest

from aiverse_server.actions import ActionRejected, MoveTo, perform
from aiverse_server.world import Position, Resident, World, WorldMap

WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(id="a", name="a", color="#ffffff", position=Position(1, 1)),
        Resident(id="b", name="b", color="#000000", position=Position(9, 9)),
    ),
)


def test_move_to_puts_only_that_resident_at_the_target() -> None:
    after = perform(WORLD, MoveTo(resident_id="a", target=Position(3.5, 7)))
    assert [r.position for r in after.residents] == [Position(3.5, 7), Position(9, 9)]
    assert after.map == WORLD.map
    assert WORLD.residents[0].position == Position(1, 1)


@pytest.mark.parametrize(("x", "y"), [(0, 0), (10, 10), (0, 10)])
def test_move_to_accepts_the_map_edges(x: float, y: float) -> None:
    after = perform(WORLD, MoveTo(resident_id="b", target=Position(x, y)))
    assert after.residents[1].position == Position(x, y)


@pytest.mark.parametrize(("x", "y"), [(-0.1, 5), (5, 10.1), (math.nan, 5), (5, math.inf)])
def test_move_to_rejects_targets_outside_the_map(x: float, y: float) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, MoveTo(resident_id="a", target=Position(x, y)))
    assert raised.value.code == "out_of_bounds"


def test_move_to_rejects_unknown_residents() -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, MoveTo(resident_id="nobody", target=Position(5, 5)))
    assert raised.value.code == "unknown_resident"
