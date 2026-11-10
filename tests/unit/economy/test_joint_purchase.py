from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import PARCEL_SHARES, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.joint_purchase import JOINT_PURCHASE_SECONDS
from aiverse_server.economy.shares import MAX_PENDING_PER_PAIR
from aiverse_server.game.actions import (
    ActionRejected,
    BuildAd,
    LeaveGame,
    MoveTo,
    OfferJointPurchase,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.lifecycle import end_game, ranks, total_coins


def resident(resident_id: str, x: float, y: float, coins: int = 5_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(x, y), coins=coins)


# A 10 x 10 grid of parcels, L5-3 costing 2540. a stands in it, b is far away, c stands in it
# too and is poor, d is in L0-0, e is in L5-3 as well.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 55, 35),
        resident("b", 5, 5),
        resident("c", 56, 36, coins=100),
        resident("d", 5, 6),
        resident("e", 57, 37),
    ),
)
PRICE = 2_540


def propose(
    world: World = WORLD, pays: int = 1_000, shares: int = 40, who: str = "a", partner: str = "b"
) -> World:
    return perform(world, OfferJointPurchase(who, partner, pays, shares))


def coins(world: World, *ids: str) -> tuple[int, ...]:
    return tuple(coin_balance(world, resident_id) for resident_id in ids)


def test_proposing_locks_no_coins_and_buys_nothing() -> None:
    world = propose()

    assert coins(world, "a", "b") == (5_000, 5_000)
    assert total_coins(world, "a") == 5_000
    assert world.parcels == ()
    assert len(world.events.pending) == 1


def test_accepting_buys_the_parcel_for_both_with_shares_as_agreed() -> None:
    world = propose()

    bought = perform(world, RespondToEvent("b", 1, True))

    assert bought.parcels == (Parcel(5, 3, "a", shares=(("a", 60), ("b", 40))),)
    assert coins(bought, "a", "b") == (5_000 - 1_540, 5_000 - 1_000)
    paid = [(t.payer, t.payee, t.amount, t.channel) for t in bought.ledger.transfers]
    assert sorted(paid) == [("a", SYSTEM, 1_540, "land"), ("b", SYSTEM, 1_000, "land")]
    assert bought.events.pending == ()
    assert total_coins(bought, "a") + total_coins(bought, "b") == 10_000 - PRICE


def test_the_partner_pays_nothing_to_get_shares_for_free_or_everything_to_fund_it() -> None:
    free = perform(propose(pays=0, shares=10), RespondToEvent("b", 1, True))
    funded = perform(propose(pays=PRICE, shares=50), RespondToEvent("b", 1, True))

    assert coins(free, "a", "b") == (5_000 - PRICE, 5_000)
    assert [t.payer for t in free.ledger.transfers] == ["a"]
    assert coins(funded, "a", "b") == (5_000, 5_000 - PRICE)
    assert [t.payer for t in funded.ledger.transfers] == ["b"]
    assert funded.parcels[0].shares == (("a", 50), ("b", 50))


@pytest.mark.parametrize(
    "ending",
    [
        RespondToEvent("b", 1, False),
        WithdrawEvent("a", 1),
        LeaveGame("a"),
    ],
)
def test_nothing_changes_hands_when_the_offer_ends_without_a_purchase(
    ending: RespondToEvent | WithdrawEvent | LeaveGame,
) -> None:
    world = propose()

    closed = perform(world, ending)

    assert closed.parcels == ()
    assert coins(closed, "a", "b") == (5_000, 5_000)
    assert (closed.events.pending, closed.ledger.transfers) == ((), ())


def test_nothing_changes_hands_when_the_game_ends_first() -> None:
    world = propose()

    ended = end_game(world)

    assert ended.parcels == ()
    assert coins(ended, "a", "b") == (5_000, 5_000)
    assert ended.events.pending == ()


def test_someone_buying_the_parcel_first_makes_accepting_fail_but_it_can_be_withdrawn() -> None:
    world = replace(propose(), parcels=(Parcel(5, 3, "c"),))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, RespondToEvent("b", 1, True))
    withdrawn = perform(world, WithdrawEvent("a", 1))

    assert rejected.value.code == "parcel_owned"
    assert len(world.events.pending) == 1
    assert coin_balance(withdrawn, "a") == 5_000
    assert withdrawn.parcels == (Parcel(5, 3, "c"),)


def test_a_partner_who_cannot_pay_leaves_the_offer_pending_and_the_world_unchanged() -> None:
    world = propose(partner="c")

    with pytest.raises(ActionRejected) as rejected:
        perform(world, RespondToEvent("c", 1, True))

    assert rejected.value.code == "not_enough_coins"
    assert (world.parcels, len(world.events.pending)) == ((), 1)
    assert coin_balance(world, "c") == 100


@pytest.mark.parametrize(
    ("pays", "shares", "code"),
    [
        (1_000, 0, "invalid_shares"),
        (1_000, PARCEL_SHARES, "invalid_shares"),
        (1_000, -1, "invalid_shares"),
        (1_000, True, "invalid_shares"),
        (1_000, 40.0, "invalid_shares"),
        (-1, 40, "invalid_shares"),
        (PRICE + 1, 40, "invalid_shares"),
        (1_000.0, 40, "invalid_shares"),
        (True, 40, "invalid_shares"),
    ],
)
def test_terms_that_make_no_sense_are_refused(pays: int, shares: int, code: str) -> None:
    with pytest.raises(ActionRejected) as rejected:
        propose(pays=pays, shares=shares)

    assert rejected.value.code == code


def test_the_proposer_must_afford_their_part_and_the_parcel_must_be_free() -> None:
    with pytest.raises(ActionRejected) as short:
        propose(who="c", pays=0, shares=10)
    with pytest.raises(ActionRejected) as owned:
        propose(replace(WORLD, parcels=(Parcel(5, 3, "d"),)))

    assert (short.value.code, owned.value.code) == ("not_enough_coins", "parcel_owned")


def test_a_resident_cannot_buy_with_themselves_or_with_someone_who_left() -> None:
    gone = perform(WORLD, LeaveGame("b"))

    with pytest.raises(ActionRejected) as to_self:
        propose(partner="a")
    with pytest.raises(ActionRejected) as to_leaver:
        propose(gone)

    assert (to_self.value.code, to_leaver.value.code) == ("self_addressed", "resident_left")


def test_a_walking_proposer_is_refused_since_they_are_not_standing_in_the_parcel() -> None:
    walking = perform(WORLD, MoveTo("a", Position(90, 90)))

    with pytest.raises(ActionRejected) as rejected:
        propose(walking)

    assert rejected.value.code == "busy"


def test_a_fourth_joint_purchase_waiting_for_the_same_partner_is_refused() -> None:
    world = WORLD
    for _ in range(MAX_PENDING_PER_PAIR):
        world = propose(world, pays=PRICE)

    with pytest.raises(ActionRejected) as rejected:
        propose(world, pays=PRICE)

    assert rejected.value.code == "too_many_offers"


def test_the_limit_on_waiting_joint_purchases_is_per_partner() -> None:
    world = WORLD
    for _ in range(MAX_PENDING_PER_PAIR):
        world = propose(world, pays=PRICE)

    another = propose(world, pays=PRICE, partner="d")

    assert len(another.events.pending) == MAX_PENDING_PER_PAIR + 1


def test_nothing_changes_hands_when_the_partner_leaves() -> None:
    world = propose()

    closed = perform(world, LeaveGame("b"))

    assert coins(closed, "a", "b") == (5_000, 5_000)
    assert (closed.parcels, closed.events.pending) == ((), ())


def test_of_two_joint_purchases_for_the_same_parcel_the_first_accepted_wins() -> None:
    world = propose(partner="b")
    world = propose(world, who="e", partner="d", pays=0, shares=10)
    bought = perform(world, RespondToEvent("b", 1, True))

    with pytest.raises(ActionRejected) as late:
        perform(bought, RespondToEvent("d", 2, True))
    withdrawn = perform(bought, WithdrawEvent("e", 2))

    assert late.value.code == "parcel_owned"
    assert bought.parcels[0].owner_id == "a"
    assert coins(withdrawn, "e") == (5_000,)
    assert withdrawn.events.pending == ()


def test_the_ranking_does_not_drop_while_a_joint_purchase_waits() -> None:
    before = ranks(WORLD)

    world = propose(pays=0)

    assert ranks(world) == before
    assert total_coins(world, "a") == 5_000


def test_both_holders_of_a_jointly_bought_parcel_may_build_on_it() -> None:
    bought = perform(propose(pays=PRICE, shares=99), RespondToEvent("b", 1, True))

    by_partner = perform(bought, BuildAd("b", "L5-3", "self"))
    by_buyer = perform(bought, BuildAd("a", "L5-3", "self"))

    assert [job.owner_id for job in by_partner.compute.jobs] == ["b"]
    assert [job.owner_id for job in by_buyer.compute.jobs] == ["a"]


def test_a_proposer_who_spent_their_coins_meanwhile_makes_accepting_fail_but_it_stays_pending() -> (
    None
):
    world = propose()
    broke = replace(
        world,
        residents=tuple(replace(r, coins=0) if r.id == "a" else r for r in world.residents),
    )

    with pytest.raises(ActionRejected) as rejected:
        perform(broke, RespondToEvent("b", 1, True))

    assert rejected.value.code == "not_enough_coins"
    assert "'a'" in rejected.value.message
    assert (broke.parcels, len(broke.events.pending)) == ((), 1)


def test_an_unanswered_offer_lapses_and_can_no_longer_be_accepted() -> None:
    world = propose()
    lapses_at = next_due(world)

    before = settle_due(replace(world, time=JOINT_PURCHASE_SECONDS - 0.1))
    after = settle_due(replace(world, time=JOINT_PURCHASE_SECONDS))
    with pytest.raises(ActionRejected) as late:
        perform(after, RespondToEvent("b", 1, True))

    assert lapses_at == JOINT_PURCHASE_SECONDS
    assert len(before.events.pending) == 1
    assert after.events.pending == ()
    assert late.value.code == "unknown_event"
    assert coins(after, "a", "b") == (5_000, 5_000)


def test_an_offer_accepted_just_before_it_lapses_still_buys_the_parcel() -> None:
    world = propose()

    late = settle_due(replace(world, time=JOINT_PURCHASE_SECONDS - 0.1))
    bought = perform(late, RespondToEvent("b", 1, True))

    assert bought.parcels[0].owner_id == "a"


def test_an_offer_sent_later_in_the_game_lapses_that_long_after_it_was_sent() -> None:
    world = propose(replace(WORLD, time=100.0))

    still = settle_due(replace(world, time=100.0 + JOINT_PURCHASE_SECONDS - 0.1))
    gone = settle_due(replace(world, time=100.0 + JOINT_PURCHASE_SECONDS))

    assert len(still.events.pending) == 1
    assert gone.events.pending == ()
