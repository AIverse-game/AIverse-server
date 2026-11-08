from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import store_item
from aiverse_server.core.land import AD_SLOT_CAPACITY, Facility, Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.ads import CONTENT_ITEM, CONTENT_LIFETIME
from aiverse_server.game.actions import ActionRejected, MoveTo, PlaceContent, perform


def resident(resident_id: str, content: int = 0) -> Resident:
    world = World(
        map=WorldMap(size=150), residents=(Resident(resident_id, "n", "#123456", Position(0, 0)),)
    )
    if content:
        world = store_item(world, resident_id, CONTENT_ITEM, content)
    return replace(world.residents[0], coins=1_000)


def ad(contents: tuple[float, ...] = ()) -> Facility:
    return Facility("ad", 0.0, contents)


# The map is 150 wide, so parcel (7, 7) sits at the middle, (7, 8) is next to it, (0, 0) is in the
# far corner. a owns the middle one and has five pieces of content, b owns the neighbour.
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        resident("a", content=5),
        resident("b"),
    ),
    parcels=(
        Parcel(7, 7, "a", ad()),
        Parcel(7, 8, "b", ad()),
        Parcel(0, 0, "a", ad()),
        Parcel(1, 0, "b"),
    ),
)


def facility(world: World, parcel_id: str) -> Facility:
    found = next(p.facility for p in world.parcels if p.id == parcel_id)
    assert found is not None
    return found


def test_content_put_in_a_slot_is_shown_for_its_lifetime_from_then() -> None:
    world = replace(WORLD, time=10.0)

    placed = perform(perform(world, PlaceContent("a", "L7-7")), PlaceContent("a", "L7-7"))

    slot = facility(placed, "L7-7")
    assert slot.contents == (10.0 + CONTENT_LIFETIME,) * 2
    assert placed.residents[0].inventory.count(CONTENT_ITEM) == 3


def test_a_slot_shows_only_so_much_and_stale_content_makes_room() -> None:
    full = world = WORLD
    for _ in range(AD_SLOT_CAPACITY):
        full = perform(full, PlaceContent("a", "L7-7"))
    world = store_item(full, "a", CONTENT_ITEM)

    with pytest.raises(ActionRejected) as refused:
        perform(world, PlaceContent("a", "L7-7"))
    later = perform(replace(world, time=CONTENT_LIFETIME), PlaceContent("a", "L7-7"))

    assert refused.value.code == "slot_full"
    assert facility(later, "L7-7").contents == (2 * CONTENT_LIFETIME,)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (PlaceContent("a", "L7-8"), "not_your_parcel"),
        (PlaceContent("a", "L9-9"), "not_your_parcel"),
        (PlaceContent("nobody", "L7-7"), "unknown_resident"),
        (PlaceContent("a", "L1-0"), "not_your_parcel"),
        (PlaceContent("b", "L1-0"), "no_content_facility"),
        (PlaceContent("b", "L7-8"), "not_enough_items"),
    ],
)
def test_content_that_cannot_be_placed_is_refused(action: PlaceContent, code: str) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, action)

    assert rejected.value.code == code


def test_a_walking_resident_can_still_place_content() -> None:
    walking = perform(WORLD, MoveTo("a", Position(100, 100)))

    placed = perform(walking, PlaceContent("a", "L7-7"))

    assert len(facility(placed, "L7-7").contents) == 1


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Facility("ad", 0.0, (0.0,) * (AD_SLOT_CAPACITY + 1)), "over the limit"),
        (lambda: Facility("ad", 0.0, (float("inf"),)), "not finite"),
    ],
)
def test_impossible_facilities_are_rejected(build: Callable[[], Facility], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()
