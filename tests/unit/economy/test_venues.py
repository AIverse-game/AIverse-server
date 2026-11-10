"""Game halls and workspaces: built by generative jobs, a game hall shows games, and neither is
paid yet."""

from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance, store_item
from aiverse_server.core.land import AD_SLOT_CAPACITY, Facility, Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.ads import (
    CONTENT_ITEM,
    CONTENT_LIFETIME,
    GAME_HALF_LIFE,
    GAME_ITEM,
    GAME_LIFETIME,
    appeal,
)
from aiverse_server.economy.ads import (
    shown as shown_pieces,
)
from aiverse_server.economy.compute import (
    GAME_WORKLOAD,
    NODE_RATE,
    NODE_SECONDS,
    SELF_RATE,
    VENUE_WORKLOAD,
    on_demand_cost,
)
from aiverse_server.game.actions import (
    ActionRejected,
    BuildVenue,
    BuyNode,
    MakeContent,
    PlaceContent,
    perform,
)
from aiverse_server.game.clock import next_due, settle_due

# a owns L7-7 at the middle of the 150 map with an ad slot, a game hall on L2-2, and L7-8 and
# L1-1 bare. b owns L3-3 bare. Both are rich.
AD = Facility("ad", 0.0)
HALL = Facility("game_hall", 0.0)
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        Resident("a", "a", "#123456", Position(75, 75), coins=5_000),
        Resident("b", "b", "#123456", Position(20, 80), coins=5_000),
    ),
    parcels=(
        Parcel(7, 7, "a", AD),
        Parcel(7, 8, "a"),
        Parcel(1, 1, "a"),
        Parcel(2, 2, "a", HALL),
        Parcel(3, 3, "b"),
    ),
)


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def facility(world: World, parcel_id: str) -> Facility | None:
    return next(p.facility for p in world.parcels if p.id == parcel_id)


def carried(world: World, resident_id: str, item: str) -> int:
    resident = next(r for r in world.residents if r.id == resident_id)
    return resident.inventory.count(item)


@pytest.mark.parametrize("kind", ["game_hall", "workspace"])
def test_a_venue_appears_when_its_job_ends_on_the_own_compute(kind: str) -> None:
    building = perform(WORLD, BuildVenue("a", "L7-8", kind, "self"))  # type: ignore[arg-type]

    before = at(building, VENUE_WORKLOAD / SELF_RATE - 1)
    after = at(building, VENUE_WORKLOAD / SELF_RATE)

    assert facility(before, "L7-8") is None
    built = facility(after, "L7-8")
    assert built is not None and built.kind == kind and built.contents == ()
    assert coin_balance(after, "a") == 5_000


def test_a_hired_node_builds_a_venue_faster_for_coins() -> None:
    hired = perform(WORLD, BuildVenue("a", "L7-8", "game_hall", "on_demand"))

    done = at(hired, VENUE_WORKLOAD / NODE_RATE)

    assert coin_balance(hired, "a") == 5_000 - on_demand_cost(VENUE_WORKLOAD)
    built = facility(done, "L7-8")
    assert built is not None and built.kind == "game_hall"


def test_a_venue_needs_a_generative_node_and_a_bare_parcel_of_the_own() -> None:
    world = perform(WORLD, BuyNode("a", "render", NODE_SECONDS[0]))

    with pytest.raises(ActionRejected) as wrong_node:
        perform(world, BuildVenue("a", "L7-8", "game_hall", "subscription", 1))
    with pytest.raises(ActionRejected) as built:
        perform(WORLD, BuildVenue("a", "L7-7", "workspace", "self"))
    with pytest.raises(ActionRejected) as foreign:
        perform(WORLD, BuildVenue("a", "L3-3", "workspace", "self"))
    with pytest.raises(ActionRejected) as unknown:
        perform(WORLD, BuildVenue("a", "L7-8", "casino", "self"))  # type: ignore[arg-type]

    assert wrong_node.value.code == "wrong_node_kind"
    assert built.value.code == "parcel_built"
    assert foreign.value.code == "not_your_parcel"
    assert unknown.value.code == "unknown_venue_kind"


def test_a_game_is_rendered_into_the_inventory_as_its_own_item() -> None:
    making = perform(WORLD, MakeContent("b", "self", content="game"))

    done = at(making, GAME_WORKLOAD / SELF_RATE)

    assert carried(done, "b", GAME_ITEM) == 1
    assert carried(done, "b", CONTENT_ITEM) == 0


def test_a_game_needs_a_render_node_and_a_known_kind() -> None:
    world = perform(WORLD, BuyNode("b", "generative", NODE_SECONDS[0]))

    with pytest.raises(ActionRejected) as wrong_node:
        perform(world, MakeContent("b", "subscription", 1, "game"))
    with pytest.raises(ActionRejected) as unknown:
        perform(WORLD, MakeContent("b", "self", content="film"))  # type: ignore[arg-type]

    assert wrong_node.value.code == "wrong_node_kind"
    assert unknown.value.code == "unknown_content_kind"


def stocked(game: int = 0, ad: int = 0) -> World:
    world = WORLD
    if game:
        world = store_item(world, "a", GAME_ITEM, game)
    if ad:
        world = store_item(world, "a", CONTENT_ITEM, ad)
    return world


def test_a_game_placed_in_a_game_hall_never_goes_stale() -> None:
    world = at(stocked(game=2), 10.0)

    placed = perform(world, PlaceContent("a", "L2-2"))
    much_later = at(placed, 10.0 + 100 * CONTENT_LIFETIME)

    hall = facility(placed, "L2-2")
    assert hall is not None and hall.contents == (10.0 + GAME_LIFETIME,)
    assert carried(placed, "a", GAME_ITEM) == 1
    assert shown_pieces(hall, much_later.time) == 1
    again = perform(much_later, PlaceContent("a", "L2-2"))
    kept = facility(again, "L2-2")
    assert kept is not None and len(kept.contents) == 2
    assert shown_pieces(kept, again.time) == 2


def test_each_facility_takes_only_its_own_kind_of_content() -> None:
    games_only = stocked(game=1)
    ads_only = stocked(ad=1)

    with pytest.raises(ActionRejected) as ad_into_hall:
        perform(ads_only, PlaceContent("a", "L2-2"))
    with pytest.raises(ActionRejected) as game_into_slot:
        perform(games_only, PlaceContent("a", "L7-7"))

    assert ad_into_hall.value.code == "not_enough_items"
    assert game_into_slot.value.code == "not_enough_items"


def test_a_workspace_and_a_bare_parcel_show_nothing() -> None:
    world = store_item(WORLD, "a", GAME_ITEM)
    workspace = replace(
        world,
        parcels=tuple(
            replace(p, facility=Facility("workspace", 0.0)) if p.id == "L7-8" else p
            for p in world.parcels
        ),
    )

    with pytest.raises(ActionRejected) as in_workspace:
        perform(workspace, PlaceContent("a", "L7-8"))
    with pytest.raises(ActionRejected) as on_bare:
        perform(workspace, PlaceContent("a", "L1-1"))

    assert in_workspace.value.code == "no_content_facility"
    assert on_bare.value.code == "no_content_facility"


def test_a_full_game_hall_drops_its_oldest_game_to_take_a_new_one() -> None:
    world = stocked(game=AD_SLOT_CAPACITY + 1)
    for time in range(AD_SLOT_CAPACITY):
        world = perform(at(world, float(time)), PlaceContent("a", "L2-2"))

    replaced = perform(at(world, 100.0), PlaceContent("a", "L2-2"))

    hall = next(p.facility for p in replaced.parcels if p.id == "L2-2")
    assert hall is not None
    assert hall.contents == tuple(GAME_LIFETIME + time for time in (1.0, 2.0, 3.0, 4.0, 100.0))


def test_a_game_draws_half_as_much_for_every_half_life_it_has_been_in_the_hall() -> None:
    hall = Facility("game_hall", 0.0, (GAME_LIFETIME + 100.0,))

    fresh = appeal(hall, 100.0)
    aged = appeal(hall, 100.0 + GAME_HALF_LIFE)
    old = appeal(hall, 100.0 + 2 * GAME_HALF_LIFE)

    assert (fresh, aged, old) == (1.0, 0.5, 0.25)


def test_an_ad_is_worth_one_piece_until_it_goes_stale() -> None:
    slot = Facility("ad", 0.0, (200.0,))

    assert (appeal(slot, 199.0), appeal(slot, 200.0)) == (1.0, 0.0)


def test_a_game_hall_earns_nothing_and_never_wakes_the_clock() -> None:
    world = stocked(game=1)
    placed = perform(at(world, 10.0), PlaceContent("a", "L2-2"))

    paid = at(placed, 120.0)

    assert coin_balance(paid, "a") == 5_000
    assert next_due(placed) is None
