"""What a model is told about the people who have seen its ad slots."""

from dataclasses import replace

from aiverse_server.agents.observation import observe
from aiverse_server.core.admissions import AdmissionBook
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.sponsors import Sponsor, SponsorBook
from aiverse_server.core.visitors import Crowd
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.residents.perception import begin_turn

SLOT = Facility("ad", 0.0, (500.0,))
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


def test_a_resident_is_told_who_pays_for_views_and_how_much_budget_each_has_now() -> None:
    sponsors = SponsorBook((Sponsor(0, "甲方", 2.0, 1.5, funds=10.0),), topped_up_at=100.0)
    world = replace(WORLD, sponsors=sponsors, time=120.0)

    perception = begin_turn(world, "a")[1]
    land = observe(world, "a", perception, 600).land

    (view,) = land.sponsors
    assert (view.name, view.price_per_view, view.budget_per_second) == ("甲方", 2.0, 1.5)
    assert view.budget_now == 40  # 10 left and 20 s at 1.5


def test_a_resident_is_told_how_many_people_went_into_their_workspace_and_what_it_earned() -> None:
    desk = Facility("workspace", 0.0)
    world = replace(
        WORLD,
        parcels=(*WORLD.parcels, Parcel(2, 2, "a", desk)),
        admissions=AdmissionBook(admitted=(("L2-2", 7.26),), last_paid=(("L2-2", 28),)),
    )

    perception = begin_turn(world, "a")[1]
    land = observe(world, "a", perception, 600).land

    mine = {parcel.id: parcel for parcel in land.mine}["L2-2"]
    assert (mine.facility, mine.people_admitted, mine.income_last_payday) == ("workspace", 7.3, 28)
    assert mine.my_cut_last_payday == 28
