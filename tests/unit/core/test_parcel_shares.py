from dataclasses import replace

import pytest

from aiverse_server.core.land import PARCEL_SHARES, Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap


def test_a_parcel_is_held_whole_by_its_owner_unless_said_otherwise() -> None:
    parcel = Parcel(1, 2, "a")

    assert parcel.shares == (("a", PARCEL_SHARES),)
    assert (parcel.held_by("a"), parcel.held_by("b")) == (PARCEL_SHARES, 0)


def test_shares_are_stored_sorted_by_holder() -> None:
    parcel = Parcel(1, 2, "a", shares=(("c", 10), ("a", 60), ("b", 30)))

    assert parcel.shares == (("a", 60), ("b", 30), ("c", 10))


@pytest.mark.parametrize(
    "shares",
    [
        (("a", 50), ("b", 49)),
        (("a", 60), ("b", 41)),
        (("a", 50), ("a", 50)),
        (("a", 100), ("b", 0)),
        (("a", 101), ("b", -1)),
        (("a", 99.0), ("b", 1)),
        (("a", True), ("b", 99)),
        (("", 100),),
    ],
)
def test_shares_must_be_whole_positive_and_add_up_to_the_parcel(
    shares: tuple[tuple[str, int], ...],
) -> None:
    with pytest.raises(ValueError):
        Parcel(1, 2, "a", shares=shares)


def test_a_world_refuses_shares_held_by_a_resident_that_does_not_exist() -> None:
    residents = tuple(Resident(n, n, "#123456", Position(1, 1)) for n in ("a", "b"))
    world = World(map=WorldMap(size=100), residents=residents)
    parcel = Parcel(1, 2, "a", shares=(("a", 50), ("z", 50)))

    with pytest.raises(ValueError, match="share holder"):
        replace(world, parcels=(parcel,))
