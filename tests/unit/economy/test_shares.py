from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance, store_item
from aiverse_server.core.land import PARCEL_SHARES, Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.ads import CONTENT_ITEM, PAY_PERIOD, slot_income
from aiverse_server.economy.shares import MAX_PENDING_PER_PAIR, split_income
from aiverse_server.game.actions import (
    ActionRejected,
    LeaveGame,
    OfferShares,
    PlaceContent,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game, total_coins


def resident(resident_id: str, coins: int = 1_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(0, 0), coins=coins)


def held(world: World, parcel_id: str) -> dict[str, int]:
    return dict(next(p for p in world.parcels if p.id == parcel_id).shares)


# a bought L7-7, in the middle of the 150 map, and its ad slot shows two pieces of content that
# stay fresh for the whole test, to be paid at the first payday; c has nothing to do with it.
SLOT = Facility("ad", 0.0, (10_000.0, 10_001.0), PAY_PERIOD)
WORLD = store_item(
    World(
        map=WorldMap(size=150),
        residents=(resident("a"), resident("b"), resident("c", coins=5)),
        parcels=(Parcel(7, 7, "a", SLOT), Parcel(0, 0, "b")),
    ),
    "a",
    CONTENT_ITEM,
    1,
)


def offer(world: World, shares: int, price: int, sender: str = "a", to: str = "b") -> World:
    return perform(world, OfferShares(sender, to, "L7-7", shares, price))


@pytest.mark.parametrize(
    ("holders", "income", "paid"),
    [
        ((("a", 100),), 7, {"a": 7}),
        ((("a", 60), ("b", 40)), 10, {"a": 6, "b": 4}),
        ((("a", 60), ("b", 40)), 5, {"a": 3, "b": 2}),
        # Both halves are 2.5: the leftover coin goes to the holder id that comes first.
        ((("a", 50), ("b", 50)), 5, {"a": 3, "b": 2}),
        # Thirds, 33 / 33 / 34: the leftovers go to the biggest fractions, then by id.
        ((("a", 33), ("b", 33), ("c", 34)), 100, {"a": 33, "b": 33, "c": 34}),
        ((("a", 33), ("b", 33), ("c", 34)), 1, {"c": 1}),
        ((("a", 33), ("b", 33), ("c", 34)), 2, {"c": 1, "a": 1}),
        # A holder owed less than a coin is left out.
        ((("a", 99), ("b", 1)), 10, {"a": 10}),
        ((("a", 50), ("b", 50)), 0, {}),
    ],
)
def test_income_is_split_by_shares_and_the_leftover_goes_to_the_biggest_fractions(
    holders: tuple[tuple[str, int], ...], income: int, paid: dict[str, int]
) -> None:
    parcel = Parcel(0, 0, "a", shares=holders)

    split = split_income(parcel, income)

    assert dict(split) == paid
    assert sum(part for _, part in split) == income


def test_every_holder_is_paid_their_part_of_what_visitors_pay_the_slot() -> None:
    sold = perform(offer(WORLD, 40, 0), RespondToEvent("b", 1, True))
    total = slot_income(replace(sold, time=PAY_PERIOD), sold.parcels[0])

    paid = settle_due(replace(sold, time=PAY_PERIOD))

    gains = {h: coin_balance(paid, h) - coin_balance(sold, h) for h in ("a", "b", "c")}
    assert total >= 3
    assert gains["a"] + gains["b"] == total
    assert gains["b"] in (total * 40 // 100, total * 40 // 100 + 1)
    assert gains["c"] == 0
    ledger = [t for t in paid.ledger.transfers if t.channel == "visitors"]
    assert sorted((t.payer, t.payee, t.amount) for t in ledger) == [
        (SYSTEM, "a", gains["a"]),
        (SYSTEM, "b", gains["b"]),
    ]


def test_a_holder_who_has_left_the_game_is_still_paid() -> None:
    sold = perform(offer(WORLD, 40, 0), RespondToEvent("b", 1, True))
    gone = perform(sold, LeaveGame("b"))

    paid = settle_due(replace(gone, time=PAY_PERIOD))

    assert coin_balance(paid, "b") > coin_balance(gone, "b")
    assert total_coins(end_game(paid), "b") == coin_balance(paid, "b")


def test_an_offer_moves_nothing_until_it_is_accepted() -> None:
    world = offer(WORLD, 30, 200)

    assert held(world, "L7-7") == {"a": PARCEL_SHARES}
    assert (coin_balance(world, "a"), coin_balance(world, "b")) == (1_000, 1_000)


def test_accepting_an_offer_moves_the_shares_and_the_coins() -> None:
    world = offer(WORLD, 30, 200)

    accepted = perform(world, RespondToEvent("b", 1, True))

    assert held(accepted, "L7-7") == {"a": 70, "b": 30}
    assert (coin_balance(accepted, "a"), coin_balance(accepted, "b")) == (1_200, 800)
    last = accepted.ledger.transfers[-1]
    assert (last.payer, last.payee, last.amount, last.channel) == ("b", "a", 200, "shares")
    assert accepted.events.pending == ()


def test_shares_can_be_given_away_for_nothing_without_a_ledger_entry() -> None:
    world = offer(WORLD, 100, 0)

    accepted = perform(world, RespondToEvent("b", 1, True))

    assert held(accepted, "L7-7") == {"b": PARCEL_SHARES}
    assert (coin_balance(accepted, "a"), coin_balance(accepted, "b")) == (1_000, 1_000)
    assert accepted.ledger.transfers == ()


def test_a_holder_can_pass_on_shares_they_were_given() -> None:
    world = perform(offer(WORLD, 40, 0), RespondToEvent("b", 1, True))

    passed = perform(offer(world, 25, 0, sender="b", to="c"), RespondToEvent("c", 2, True))

    assert held(passed, "L7-7") == {"a": 60, "b": 15, "c": 25}


@pytest.mark.parametrize("answer", ["reject", "withdraw"])
def test_a_refused_or_withdrawn_offer_changes_nothing_and_frees_the_shares_to_be_offered(
    answer: str,
) -> None:
    world = offer(WORLD, 80, 100)
    action = RespondToEvent("b", 1, False) if answer == "reject" else WithdrawEvent("a", 1)

    closed = perform(world, action)

    assert held(closed, "L7-7") == {"a": PARCEL_SHARES}
    assert (coin_balance(closed, "a"), coin_balance(closed, "b")) == (1_000, 1_000)
    assert held(offer(closed, 90, 100), "L7-7") == {"a": PARCEL_SHARES}


def test_shares_already_offered_cannot_be_offered_again() -> None:
    world = offer(WORLD, 80, 100)

    with pytest.raises(ActionRejected) as rejected:
        offer(world, 21, 100, to="c")

    assert rejected.value.code == "not_enough_shares"
    assert len(offer(world, 20, 100, to="c").events.pending) == 2


def test_a_fourth_offer_waiting_for_the_same_recipient_is_refused() -> None:
    world = WORLD
    for _ in range(MAX_PENDING_PER_PAIR):
        world = offer(world, 1, 0)

    with pytest.raises(ActionRejected) as rejected:
        offer(world, 1, 0)

    assert rejected.value.code == "too_many_offers"
    assert len(offer(world, 1, 0, to="c").events.pending) == MAX_PENDING_PER_PAIR + 1
    answered = perform(world, RespondToEvent("b", 1, False))
    assert len(offer(answered, 1, 0).events.pending) == MAX_PENDING_PER_PAIR


def test_an_offer_the_buyer_cannot_pay_stays_pending_and_changes_nothing() -> None:
    world = offer(WORLD, 30, 200, to="c")

    with pytest.raises(ActionRejected) as rejected:
        perform(world, RespondToEvent("c", 1, True))

    assert rejected.value.code == "not_enough_coins"
    assert held(world, "L7-7") == {"a": PARCEL_SHARES}
    assert len(world.events.pending) == 1


@pytest.mark.parametrize(
    ("sender", "parcel_id", "shares", "price", "code"),
    [
        ("a", "L7-7", 0, 10, "invalid_shares"),
        ("a", "L7-7", -5, 10, "invalid_shares"),
        ("a", "L7-7", PARCEL_SHARES + 1, 10, "invalid_shares"),
        ("a", "L7-7", True, 10, "invalid_shares"),
        ("a", "L7-7", 10, -1, "invalid_shares"),
        ("a", "L7-7", 10, 10**9 + 1, "invalid_shares"),
        ("a", "L7-7", 10, 10.5, "invalid_shares"),
        ("a", "L1-1", 10, 10, "unknown_parcel"),
        # b holds no share of a's parcel, and a none of b's.
        ("b", "L7-7", 10, 10, "not_enough_shares"),
        ("a", "L0-0", 10, 10, "not_enough_shares"),
    ],
)
def test_an_offer_that_makes_no_sense_is_refused(
    sender: str, parcel_id: str, shares: int, price: int, code: str
) -> None:
    recipient = "b" if sender == "a" else "a"

    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, OfferShares(sender, recipient, parcel_id, shares, price))

    assert rejected.value.code == code


def test_nobody_can_offer_shares_to_themselves_or_to_someone_who_left() -> None:
    gone = perform(WORLD, LeaveGame("b"))

    with pytest.raises(ActionRejected) as to_self:
        offer(WORLD, 10, 0, to="a")
    with pytest.raises(ActionRejected) as to_leaver:
        offer(gone, 10, 0)

    assert to_self.value.code == "self_addressed"
    assert to_leaver.value.code == "resident_left"


def test_leaving_the_game_calls_off_the_residents_share_offers() -> None:
    world = offer(WORLD, 30, 200)

    gone = perform(world, LeaveGame("a"))

    assert gone.events.pending == ()
    assert held(gone, "L7-7") == {"a": PARCEL_SHARES}


def test_the_buyer_of_a_parcel_keeps_running_it_after_selling_every_share() -> None:
    world = perform(offer(WORLD, 100, 0), RespondToEvent("b", 1, True))

    placed = perform(world, PlaceContent("a", "L7-7"))

    assert placed.parcels[0].facility is not None
    assert placed.parcels[0].facility.contents[-1] > world.time


def test_a_holder_who_did_not_buy_the_parcel_cannot_put_content_in_it() -> None:
    world = perform(offer(WORLD, 100, 0), RespondToEvent("b", 1, True))
    world = store_item(world, "b", CONTENT_ITEM, 1)

    with pytest.raises(ActionRejected) as rejected:
        perform(world, PlaceContent("b", "L7-7"))

    assert rejected.value.code == "not_your_parcel"
