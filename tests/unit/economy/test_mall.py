"""The mall: a 1 x 2 parcel venue that draws visitors without content, for a small fee."""

from dataclasses import replace

import pytest

from aiverse_server.core.compute import ComputeBook, Permit
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy import visitors
from aiverse_server.economy.admissions import FEES
from aiverse_server.economy.compute import (
    MALL_WORKLOAD,
    SELF_RATE,
    VENUE_WORKLOAD,
    promised_builder,
)
from aiverse_server.economy.land import claimed, facility_centre, venue_room
from aiverse_server.economy.visitors import MALL_PIECES, WORKSPACE_PIECES, visitor_weight
from aiverse_server.game.actions import ActionRejected, BuildAd, BuildVenue, perform
from aiverse_server.game.clock import settle_due

# 90 x 90 in blocks of 3 x 3 parcels, game halls and workspaces 2 x 2, a mall 1 x 2. a owns the
# block of L0-0 to L2-2.
MAP = WorldMap(size=90, block_cells=3, venue_cells=2)
BLOCK = tuple((col, row) for row in range(3) for col in range(3))
WORLD = World(
    map=MAP,
    residents=(Resident("a", "a", "#123456", Position(15, 15), coins=5_000),),
    parcels=tuple(Parcel(col, row, "a") for col, row in BLOCK),
)
MALL = Facility("mall", 0.0)
ENTRANCE = Position(50, 50)


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def test_a_mall_takes_one_parcel_across_and_two_down_from_its_north_west_corner() -> None:
    building = perform(WORLD, BuildVenue("a", "L1-0", "mall", "self"))

    assert {cell for cell, holder in claimed(building).items() if holder == "L1-0"} == {
        (1, 0),
        (1, 1),
    }


def test_a_mall_lies_on_the_middle_of_its_two_parcels() -> None:
    assert facility_centre(MAP, 1, 0, "mall") == Position(15, 10)


def test_a_mall_is_refused_on_the_last_row_of_a_block_but_not_on_the_last_column() -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, BuildVenue("a", "L0-2", "mall", "self"))
    beside = perform(WORLD, BuildVenue("a", "L2-0", "mall", "self"))

    assert raised.value.code == "no_room_for_venue"
    assert [job.parcel_id for job in beside.compute.jobs] == ["L2-0"]


def test_a_mall_leaves_the_rest_of_the_block_free_for_other_facilities() -> None:
    building = perform(WORLD, BuildVenue("a", "L0-0", "mall", "self"))

    with pytest.raises(ActionRejected) as covered:
        perform(building, BuildAd("a", "L0-1", "self"))
    free = perform(building, BuildAd("a", "L1-0", "self"))

    assert covered.value.code == "parcel_covered"
    assert [job.parcel_id for job in free.compute.jobs] == ["L0-0", "L1-0"]


def test_venue_room_for_a_mall_is_a_different_question_from_one_for_a_game_hall() -> None:
    taken = claimed(WORLD)
    parcels = {parcel.id: parcel for parcel in WORLD.parcels}

    hall = {id_: venue_room(WORLD, parcels[id_], taken) for id_ in ("L2-0", "L0-1")}
    mall = {id_: venue_room(WORLD, parcels[id_], taken, "mall") for id_ in ("L2-0", "L0-1", "L0-2")}

    assert hall == {"L2-0": False, "L0-1": True}
    assert mall == {"L2-0": True, "L0-1": True, "L0-2": False}


def test_a_mall_costs_less_to_build_than_a_game_hall_and_appears_when_its_job_ends() -> None:
    building = perform(WORLD, BuildVenue("a", "L0-0", "mall", "self"))

    built = at(building, MALL_WORKLOAD / SELF_RATE)

    assert MALL_WORKLOAD < VENUE_WORKLOAD
    assert next(p for p in built.parcels if p.id == "L0-0").facility == Facility("mall", built.time)


def test_a_mall_draws_less_and_earns_less_than_the_other_venues() -> None:
    assert MALL_PIECES < WORKSPACE_PIECES
    assert FEES["mall"] < FEES["workspace"] < FEES["game_hall"]


def mall_world(*parcels: Parcel, crowd: Crowd) -> World:
    return World(
        map=WorldMap(size=100),
        residents=(Resident("a", "a", "#123456", Position(5, 5), coins=1_000),),
        parcels=parcels,
        crowd=crowd,
    )


@pytest.fixture
def no_working_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visitors, "ROUTE_SHARE", 0.0)


def arriving(seed: int) -> Crowd:
    return Crowd(seed=seed, visitors=(Visitor(0, "away", 5.0, ENTRANCE, ENTRANCE),))


@pytest.mark.usefixtures("no_working_route")
def test_a_tourist_goes_into_a_mall_with_no_content_and_pays_the_small_fee() -> None:
    # The mall on L5-3 and L5-4, so its middle is (55, 40).
    world = mall_world(Parcel(5, 3, "a", MALL), crowd=arriving(0))
    walking = at(world, 5.0).crowd.visitors[0]

    inside = at(at(world, 5.0), walking.until)

    assert (walking.goal, walking.stop) == ("place", "L5-3")
    assert dict(inside.admissions.unpaid) == {
        "L5-3": pytest.approx(FEES["mall"] * visitor_weight(walking.until))
    }


@pytest.mark.usefixtures("no_working_route")
def test_tourists_pick_a_workspace_over_a_mall_about_as_near_more_often() -> None:
    # The two stand a few units from the entrance, 7 and 11 units; walking costs next to nothing.
    workspace, mall = Parcel(4, 4, "a", Facility("workspace", 0.0)), Parcel(5, 5, "a", MALL)
    picks = []
    for seed in range(200):
        world = mall_world(workspace, mall, crowd=arriving(seed))
        picks.append(at(world, 5.0).crowd.visitors[0].stop)

    assert picks.count("L4-4") > 1.3 * picks.count("L5-5") > 0


def with_permit(parcel_id: str, kind: str) -> World:
    permit = Permit(parcel_id, kind, "a", "b", 0, 100.0)  # type: ignore[arg-type]
    builder = Resident("b", "b", "#123456", Position(45, 15), coins=5_000)
    return replace(
        WORLD,
        residents=(*WORLD.residents, builder),
        compute=ComputeBook((), (), 1, 1, (permit,)),
    )


def test_a_mall_permit_promises_the_parcel_south_of_it_to_a_game_hall_square() -> None:
    permitted = with_permit("L0-1", "mall")

    # The square at L0-0 would take up L0-1; the one at L1-0 would not.
    assert promised_builder(permitted, "L0-0", "game_hall") == "b"
    assert promised_builder(permitted, "L1-0", "game_hall") is None
    assert promised_builder(permitted, "L0-2", None) == "b"
    assert promised_builder(permitted, "L2-0", "mall") is None


def test_a_game_hall_permit_promises_its_whole_square_to_a_mall() -> None:
    permitted = with_permit("L0-0", "game_hall")

    assert promised_builder(permitted, "L1-1", "mall") == "b"
    assert promised_builder(permitted, "L2-0", "mall") is None


def test_a_running_mall_job_takes_up_both_parcels() -> None:
    building = perform(WORLD, BuildVenue("a", "L2-0", "mall", "self"))

    assert {cell for cell, holder in claimed(building).items() if holder == "L2-0"} == {
        (2, 0),
        (2, 1),
    }
