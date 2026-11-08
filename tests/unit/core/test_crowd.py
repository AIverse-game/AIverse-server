import math

import pytest

from aiverse_server.core.land import Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap

HERE = Position(10, 10)


def visitor(**kwargs: object) -> Visitor:
    fields: dict[str, object] = {"id": 0, "phase": "away", "until": 1.0, "at": HERE, "target": HERE}
    return Visitor(**{**fields, **kwargs})  # type: ignore[arg-type]


def test_a_visitor_has_a_stop_exactly_when_it_is_going_to_a_slot() -> None:
    with pytest.raises(ValueError, match="stop"):
        visitor(goal="slot")
    with pytest.raises(ValueError, match="stop"):
        visitor(goal="wander", stop="L1-1")


@pytest.mark.parametrize(
    "bad",
    [
        {"phase": "dancing"},
        {"goal": "teleport"},
        {"until": math.nan},
        {"departs_at": math.inf},
        {"target": Position(math.nan, 0)},
        {"trips": 1},
        {"stops_left": -1},
        {"seen": (("L1-1", math.nan),)},
    ],
)
def test_a_visitor_with_nonsense_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        visitor(**bad)


def test_a_crowd_lists_visitors_in_order_and_views_by_parcel_once() -> None:
    with pytest.raises(ValueError, match="unique ids"):
        Crowd(visitors=(visitor(id=1), visitor(id=0)))
    with pytest.raises(ValueError, match="once"):
        Crowd(views=(("L1-1", 1.0), ("L1-1", 2.0)))
    with pytest.raises(ValueError, match="positive"):
        Crowd(views=(("L1-1", 0.0),))


def test_views_of_a_parcel_are_found_and_zero_when_nobody_saw_it() -> None:
    crowd = Crowd(views=(("L1-1", 2.5),))

    assert (crowd.viewed("L1-1"), crowd.viewed("L2-2")) == (2.5, 0.0)


def test_views_of_land_nobody_owns_are_refused() -> None:
    residents = (Resident("a", "a", "#123456", HERE),)

    with pytest.raises(ValueError, match="nobody owns"):
        World(WorldMap(100), residents, crowd=Crowd(views=(("L1-1", 1.0),)))
    World(
        WorldMap(100), residents, parcels=(Parcel(1, 1, "a"),), crowd=Crowd(views=(("L1-1", 1.0),))
    )
