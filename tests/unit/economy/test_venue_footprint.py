"""A game hall or a workspace takes up a square of parcels, not one: what it keeps other
facilities off, where it may be built, and where its middle is."""

from dataclasses import replace

import pytest

from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.compute import SELF_RATE, VENUE_WORKLOAD
from aiverse_server.economy.land import claimed, facility_centre, venue_room
from aiverse_server.game.actions import ActionRejected, BuildAd, BuildPortal, BuildVenue, perform
from aiverse_server.game.clock import settle_due

# 90 x 90 in blocks of 3 x 3 parcels, venues 2 x 2. a owns the block of L0-0 to L2-2, b the
# block next to it.
MAP = WorldMap(size=90, block_cells=3, venue_cells=2)
BLOCK = tuple((col, row) for row in range(3) for col in range(3))
WORLD = World(
    map=MAP,
    residents=(
        Resident("a", "a", "#123456", Position(15, 15), coins=5_000),
        Resident("b", "b", "#123456", Position(45, 15), coins=5_000),
    ),
    parcels=(
        *(Parcel(col, row, "a") for col, row in BLOCK),
        *(Parcel(col + 3, row, "b") for col, row in BLOCK),
    ),
)


def reject(world: World, action: BuildAd | BuildPortal | BuildVenue) -> str:
    with pytest.raises(ActionRejected) as raised:
        perform(world, action)
    return raised.value.code


def with_workspace_at_origin() -> World:
    building = perform(WORLD, BuildVenue("a", "L0-0", "workspace", "self"))
    return settle_due(replace(building, time=VENUE_WORKLOAD / SELF_RATE))


def test_a_venue_is_refused_where_its_square_would_leave_the_block() -> None:
    codes = [
        reject(WORLD, BuildVenue("a", f"L{col}-{row}", "game_hall", "self"))
        for col, row in [(2, 0), (0, 2), (2, 2)]
    ]

    assert codes == ["no_room_for_venue"] * 3


def test_a_venue_in_the_middle_of_a_block_takes_up_the_square_below_and_right_of_it() -> None:
    building = perform(WORLD, BuildVenue("a", "L1-1", "workspace", "self"))

    assert {cell for cell, holder in claimed(building).items() if holder == "L1-1"} == {
        (1, 1),
        (2, 1),
        (1, 2),
        (2, 2),
    }


def test_nothing_else_is_built_on_a_parcel_a_venue_building_takes_up() -> None:
    building = perform(WORLD, BuildVenue("a", "L0-0", "workspace", "self"))

    covered = [reject(building, BuildAd("a", id_, "self")) for id_ in ("L1-0", "L0-1", "L1-1")]
    portal = reject(building, BuildPortal("a", "L1-1", "self"))
    second_venue = reject(building, BuildVenue("a", "L1-1", "game_hall", "self"))

    assert covered == ["parcel_covered"] * 3
    assert (portal, second_venue) == ("parcel_covered", "parcel_covered")


def test_a_finished_venue_keeps_the_parcels_taken_but_the_rest_of_the_block_is_free() -> None:
    built = with_workspace_at_origin()

    with pytest.raises(ActionRejected) as covered:
        perform(built, BuildAd("a", "L1-1", "self"))
    free = perform(built, BuildAd("a", "L2-0", "self"))

    assert covered.value.code == "parcel_covered"
    assert [job.parcel_id for job in free.compute.jobs] == ["L2-0"]
    assert reject(built, BuildAd("a", "L0-0", "self")) == "parcel_built"


def test_a_venue_is_refused_where_a_facility_or_job_already_sits_in_its_square() -> None:
    with_ad = perform(WORLD, BuildAd("a", "L1-1", "self"))
    with_portal = perform(WORLD, BuildPortal("a", "L0-1", "self"))

    assert reject(with_ad, BuildVenue("a", "L0-0", "workspace", "on_demand")) == "parcel_covered"
    assert (
        reject(with_portal, BuildVenue("a", "L0-0", "workspace", "on_demand")) == "parcel_covered"
    )


def test_venue_room_says_where_a_venue_could_start_now() -> None:
    built = with_workspace_at_origin()
    taken = claimed(built)
    parcels = {parcel.id: parcel for parcel in built.parcels}

    room = {id_: venue_room(built, parcels[id_], taken) for id_ in ("L0-0", "L1-1", "L3-0", "L4-1")}

    assert room == {"L0-0": False, "L1-1": False, "L3-0": True, "L4-1": True}


def test_a_venue_lies_on_the_middle_of_its_square_and_other_facilities_on_their_parcel() -> None:
    hall = facility_centre(MAP, 1, 0, "game_hall")
    workspace = facility_centre(MAP, 4, 1, "workspace")
    ad = facility_centre(MAP, 1, 0, "ad")
    bare = facility_centre(MAP, 1, 0, None)

    assert (hall, workspace) == (Position(20, 10), Position(50, 20))
    assert (ad, bare) == (Position(15, 5), Position(15, 5))


def test_the_default_map_takes_one_parcel_for_a_venue_so_old_worlds_are_unchanged() -> None:
    plain = World(
        map=WorldMap(size=90),
        residents=WORLD.residents,
        parcels=(Parcel(0, 0, "a"), Parcel(1, 0, "a", Facility("workspace", 0.0))),
    )

    assert set(claimed(plain)) == {(1, 0)}
    assert facility_centre(plain.map, 1, 0, "workspace") == Position(15, 5)


@pytest.mark.parametrize("cells", [0, 4, 2.0])
def test_venue_cells_is_a_whole_number_from_one_to_the_block_size(cells: int) -> None:
    with pytest.raises(ValueError, match="venue_cells"):
        WorldMap(size=90, block_cells=3, venue_cells=cells)


def test_a_venue_is_refused_at_the_edge_of_a_map_that_ends_inside_a_block() -> None:
    narrow = World(
        map=WorldMap(size=40, block_cells=3, venue_cells=2),
        residents=(Resident("a", "a", "#123456", Position(5, 5), coins=5_000),),
        parcels=(Parcel(3, 0, "a"),),
    )

    assert reject(narrow, BuildVenue("a", "L3-0", "workspace", "self")) == "no_room_for_venue"
