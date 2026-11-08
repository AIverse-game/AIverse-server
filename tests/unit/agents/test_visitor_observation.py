"""What a model is told about the people who have seen its ad slots."""

from dataclasses import replace

from aiverse_server.agents.observation import observe
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.visitors import Crowd
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.ads import PAY_PERIOD
from aiverse_server.residents.perception import begin_turn

SLOT = Facility("ad", 0.0, (500.0,), PAY_PERIOD)
WORLD = World(
    map=WorldMap(size=150),
    residents=(Resident("a", "甲", "#112233", Position(75, 75), coins=5_000),),
    parcels=(Parcel(7, 7, "a", SLOT), Parcel(1, 1, "a")),
)


def seen_on(world: World) -> dict[str, float]:
    perception = begin_turn(world, "a")[1]
    land = observe(world, "a", perception, 600).land
    return {parcel.id: parcel.people_seen for parcel in land.mine}


def test_a_resident_is_told_how_many_people_have_seen_each_of_their_slots() -> None:
    seen = replace(WORLD, crowd=Crowd(views=(("L7-7", 12.34),)))

    assert seen_on(seen) == {"L7-7": 12.3, "L1-1": 0.0}
    assert seen_on(WORLD) == {"L7-7": 0.0, "L1-1": 0.0}
