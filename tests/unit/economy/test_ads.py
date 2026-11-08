from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance, store_item
from aiverse_server.core.land import AD_SLOT_CAPACITY, Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.ads import (
    CONTENT_ITEM,
    CONTENT_LIFETIME,
    PAY_PER_FOOTFALL,
    PAY_PERIOD,
    next_payday,
    payday_after,
    slot_income,
)
from aiverse_server.economy.footfall import footfall
from aiverse_server.economy.land import plot
from aiverse_server.game.actions import ActionRejected, LeaveGame, MoveTo, PlaceContent, perform
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.lifecycle import total_coins


def resident(resident_id: str, content: int = 0) -> Resident:
    world = World(
        map=WorldMap(size=150), residents=(Resident(resident_id, "n", "#123456", Position(0, 0)),)
    )
    if content:
        world = store_item(world, resident_id, CONTENT_ITEM, content)
    return replace(world.residents[0], coins=1_000)


def ad(contents: tuple[float, ...] = (), next_pay_at: float = PAY_PERIOD) -> Facility:
    return Facility("ad", 0.0, contents, next_pay_at)


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


def shows(world: World, parcel_id: str, contents: int) -> World:
    parcels = tuple(
        replace(p, facility=ad(tuple(1_000.0 + n for n in range(contents))))
        if p.id == parcel_id
        else p
        for p in world.parcels
    )
    return replace(world, parcels=parcels)


def income(world: World, parcel_id: str) -> int:
    return slot_income(world, next(p for p in world.parcels if p.id == parcel_id))


def centre_footfall(world: World, parcel_id: str) -> float:
    parcel = next(p for p in world.parcels if p.id == parcel_id)
    return footfall(world, plot(world, parcel.col, parcel.row).centre)


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


def test_a_slot_showing_content_is_paid_at_the_next_payday_by_footfall_and_pieces() -> None:
    world = shows(WORLD, "L7-7", 2)
    world = replace(
        world,
        parcels=tuple(replace(p, facility=ad()) if p.id == "L7-8" else p for p in world.parcels),
    )

    before = settle_due(replace(world, time=PAY_PERIOD - 0.1))
    paid = settle_due(replace(world, time=PAY_PERIOD))

    expected = round(
        centre_footfall(replace(world, time=PAY_PERIOD), "L7-7") * 2 * PAY_PER_FOOTFALL
    )
    assert coin_balance(before, "a") == 1_000
    assert coin_balance(paid, "a") == 1_000 + expected
    last = paid.ledger.transfers[-1]
    assert (last.payer, last.payee, last.amount, last.channel) == (
        SYSTEM,
        "a",
        expected,
        "visitors",
    )
    assert expected > 0


def test_paydays_come_every_period_whatever_else_wakes_the_clock() -> None:
    world = shows(WORLD, "L7-7", 1)

    later = settle_due(replace(world, time=3 * PAY_PERIOD + 5.0))

    paid = [t for t in later.ledger.transfers if t.channel == "visitors"]
    assert [t.time for t in paid] == [PAY_PERIOD, 2 * PAY_PERIOD, 3 * PAY_PERIOD]
    assert facility(later, "L7-7").next_pay_at == 4 * PAY_PERIOD


def test_nearby_slots_showing_content_share_their_footfall_and_empty_ones_do_not() -> None:
    alone = shows(WORLD, "L7-7", 1)
    crowded = shows(alone, "L7-8", 1)

    income_alone = income(alone, "L7-7")
    income_shared = income(crowded, "L7-7")

    assert income_alone == round(centre_footfall(alone, "L7-7") * PAY_PER_FOOTFALL)
    assert income_shared == round(centre_footfall(crowded, "L7-7") / 2 * PAY_PER_FOOTFALL)


def test_a_slot_far_away_does_not_share_and_earns_less_where_fewer_visitors_pass() -> None:
    world = shows(shows(WORLD, "L7-7", 1), "L0-0", 1)

    assert income(world, "L7-7") == round(centre_footfall(world, "L7-7") * PAY_PER_FOOTFALL)
    assert 0 < income(world, "L0-0") < income(world, "L7-7")


def test_more_content_in_a_slot_earns_proportionally_more() -> None:
    one = income(shows(WORLD, "L7-7", 1), "L7-7")

    three = income(shows(WORLD, "L7-7", 3), "L7-7")

    assert three == pytest.approx(3 * one, abs=1)


def test_stale_content_earns_nothing_and_is_dropped_at_payday() -> None:
    world = replace(
        WORLD,
        parcels=(
            Parcel(7, 7, "a", ad((PAY_PERIOD - 5.0, 500.0))),
            *WORLD.parcels[1:],
        ),
    )

    paid = settle_due(replace(world, time=PAY_PERIOD))

    expected = round(centre_footfall(replace(world, time=PAY_PERIOD), "L7-7") * PAY_PER_FOOTFALL)
    assert coin_balance(paid, "a") == 1_000 + expected
    assert facility(paid, "L7-7").contents == (500.0,)


def test_a_slot_with_no_fresh_content_is_not_paid_and_does_not_wake_the_clock() -> None:
    world = replace(WORLD, parcels=(Parcel(7, 7, "a", ad((20.0,))), *WORLD.parcels[1:]))

    assert next_payday(WORLD) is None
    assert next_payday(world) is None
    assert next_due(world) is None
    assert coin_balance(settle_due(replace(world, time=300.0)), "a") == 1_000


def test_content_placed_after_a_quiet_spell_is_paid_from_the_next_payday_not_the_missed_ones() -> (
    None
):
    world = replace(WORLD, time=10 * PAY_PERIOD + 4.0)

    placed = perform(world, PlaceContent("a", "L7-7"))
    paid = settle_due(replace(placed, time=11 * PAY_PERIOD))

    assert facility(placed, "L7-7").next_pay_at == 11 * PAY_PERIOD
    assert next_payday(placed) == 11 * PAY_PERIOD
    assert len([t for t in paid.ledger.transfers if t.channel == "visitors"]) == 1


def test_an_owner_who_left_is_still_paid_for_content_already_shown() -> None:
    world = shows(WORLD, "L7-7", 1)

    left = perform(world, LeaveGame("a"))
    paid = settle_due(replace(left, time=PAY_PERIOD))

    assert coin_balance(paid, "a") > coin_balance(left, "a")
    assert total_coins(paid, "a") == coin_balance(paid, "a")


def test_neighbouring_slots_are_each_paid_their_share_at_the_same_payday() -> None:
    world = shows(shows(WORLD, "L7-7", 1), "L7-8", 3)
    at_payday = replace(world, time=PAY_PERIOD)

    paid = settle_due(at_payday)

    assert coin_balance(paid, "a") - 1_000 == income(at_payday, "L7-7")
    assert coin_balance(paid, "b") - 1_000 == income(at_payday, "L7-8")
    assert income(at_payday, "L7-8") == round(
        centre_footfall(at_payday, "L7-8") / 2 * 3 * PAY_PER_FOOTFALL
    )
    assert income(at_payday, "L7-7") == round(
        centre_footfall(at_payday, "L7-7") / 2 * PAY_PER_FOOTFALL
    )


def test_content_going_stale_exactly_at_a_payday_is_not_paid_for() -> None:
    world = replace(WORLD, parcels=(Parcel(7, 7, "a", ad((PAY_PERIOD,))), *WORLD.parcels[1:]))

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert next_payday(world) is None
    assert coin_balance(paid, "a") == 1_000


def test_a_slot_two_parcels_along_a_row_still_shares_but_a_knights_move_away_does_not() -> None:
    world = World(
        map=WorldMap(size=150),
        residents=WORLD.residents,
        parcels=(
            Parcel(7, 7, "a", ad((500.0,))),
            Parcel(7, 9, "b", ad((500.0,))),
            Parcel(8, 9, "b", ad((500.0,))),
        ),
    )

    alone = income(shows(WORLD, "L7-7", 1), "L7-7")

    assert income(world, "L7-7") == round(centre_footfall(world, "L7-7") / 2 * PAY_PER_FOOTFALL)
    assert alone > income(world, "L7-7")


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Facility("ad", 0.0, (0.0,) * (AD_SLOT_CAPACITY + 1)), "over the limit"),
        (lambda: Facility("ad", 0.0, (float("inf"),)), "not finite"),
        (lambda: Facility("ad", 0.0, (), -1.0), "is paid at"),
        (lambda: Facility("ad", 0.0, (), float("nan")), "is paid at"),
    ],
)
def test_impossible_facilities_are_rejected(build: Callable[[], Facility], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_payday_after_is_the_next_multiple_of_the_period() -> None:
    assert [payday_after(t) for t in (0.0, 0.1, 29.9, 30.0, 47.3)] == [30.0, 30.0, 30.0, 60.0, 60.0]
