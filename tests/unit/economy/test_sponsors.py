"""Sponsors: they pay ad slots for the views the scripted visitors gave them at paydays."""

from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.sponsors import Sponsor, SponsorBook
from aiverse_server.core.visitors import Crowd
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.sponsors import (
    BUDGET_CAP_SECONDS,
    PAY_PERIOD,
    default_sponsors,
    latest_payday,
    next_payday,
    pay_sponsors,
    unsettled_views,
)
from aiverse_server.economy.visitors import new_crowd
from aiverse_server.game.actions import LeaveGame, perform
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.lifecycle import total_coins

AD = Facility("ad", 0.0, (10_000.0,))


def sponsor(price: float = 2.0, rate: float = 1_000.0, funds: float = 0.0, id: int = 0) -> Sponsor:
    return Sponsor(id, f"s{id}", price, rate, funds)


def views(**by_parcel: float) -> Crowd:
    return Crowd(views=tuple(sorted((name.replace("_", "-"), n) for name, n in by_parcel.items())))


# a owns the ad slots on L7-7 and L7-8, b the one on L1-1; each has 1000 coins. The sponsors
# and the views are given by the tests.
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        Resident("a", "a", "#123456", Position(0, 0), coins=1_000),
        Resident("b", "b", "#123456", Position(0, 0), coins=1_000),
    ),
    parcels=(Parcel(7, 7, "a", AD), Parcel(7, 8, "a", AD), Parcel(1, 1, "b", AD)),
)


def with_sponsors(*sponsors: Sponsor, crowd: Crowd | None = None, time: float = 0.0) -> World:
    return replace(WORLD, sponsors=SponsorBook(sponsors), crowd=crowd or Crowd(), time=time)


def seen(a: float = 0, b: float = 0, c: float = 0) -> Crowd:
    """People who passed the slots on L7-7, L7-8 and L1-1."""
    pairs = (("L1-1", c), ("L7-7", a), ("L7-8", b))
    return Crowd(views=tuple((parcel, n) for parcel, n in pairs if n))


def coins(world: World, resident_id: str) -> int:
    return coin_balance(world, resident_id) - 1_000


def test_a_slot_is_paid_at_the_payday_for_the_people_who_passed_it_at_the_price_a_view() -> None:
    world = with_sponsors(sponsor(price=2.0), crowd=seen(a=10))

    before = settle_due(replace(world, time=PAY_PERIOD - 0.1))
    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert coins(before, "a") == 0
    assert coins(paid, "a") == 20
    last = paid.ledger.transfers[-1]
    assert (last.time, last.payer, last.payee, last.amount, last.channel) == (
        PAY_PERIOD,
        SYSTEM,
        "a",
        20,
        "sponsors",
    )
    assert paid.sponsors.last_paid == (("L7-7", 20),)


def test_every_slot_is_paid_for_its_own_views_at_the_same_payday() -> None:
    world = with_sponsors(sponsor(price=2.0), crowd=seen(a=10, b=3, c=5))

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert (coins(paid, "a"), coins(paid, "b")) == (26, 10)
    assert dict(paid.sponsors.last_paid) == {"L7-7": 20, "L7-8": 6, "L1-1": 10}


def test_views_are_paid_for_once_and_later_ones_at_the_next_payday() -> None:
    world = with_sponsors(sponsor(price=2.0), crowd=seen(a=10))
    first = settle_due(replace(world, time=PAY_PERIOD))

    more = replace(first, crowd=seen(a=15))
    second = settle_due(replace(more, time=2 * PAY_PERIOD))

    assert coins(second, "a") == 20 + 10
    assert unsettled_views(second) == {}
    assert [t.time for t in second.ledger.transfers] == [PAY_PERIOD, 2 * PAY_PERIOD]


def test_the_money_goes_to_the_holders_of_the_shares_in_proportion() -> None:
    shared = replace(WORLD.parcels[0], shares=(("a", 70), ("b", 30)))
    world = replace(
        with_sponsors(sponsor(price=1.0), crowd=seen(a=100)), parcels=(shared, *WORLD.parcels[1:])
    )

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert (coins(paid, "a"), coins(paid, "b")) == (70, 30)


def test_an_owner_who_left_is_still_paid_for_views_the_slot_got() -> None:
    world = perform(with_sponsors(sponsor(), crowd=seen(a=10)), LeaveGame("a"))

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert coins(paid, "a") == 20
    assert total_coins(paid, "a") == coin_balance(paid, "a")


def test_when_the_budgets_fall_short_every_slot_is_paid_for_the_same_share_of_its_views() -> None:
    # Budget at the first payday is 0.1 * 30 = 3 coins, enough for 3 of the 10 views.
    world = with_sponsors(sponsor(price=1.0, rate=0.1), crowd=seen(a=6, b=4))

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert dict(paid.sponsors.last_paid) == {"L7-7": 2, "L7-8": 1}
    assert sum(t.amount for t in paid.ledger.transfers) == 3


def test_views_the_budgets_could_not_pay_for_are_not_carried_over() -> None:
    world = with_sponsors(sponsor(price=1.0, rate=0.1), crowd=seen(a=10))
    first = settle_due(replace(world, time=PAY_PERIOD))

    later = settle_due(replace(first, time=5 * PAY_PERIOD))

    assert unsettled_views(first) == {}
    assert coins(later, "a") == coins(first, "a")
    assert next_payday(first) is None


def test_the_sponsors_who_pay_most_buy_the_views_first() -> None:
    # The dear one can afford 2 views (6 coins) and the cheap one the rest: 6 + 8 for 10 views.
    world = with_sponsors(
        sponsor(price=1.0, id=0), sponsor(price=3.0, rate=0.2, id=1), crowd=seen(a=10)
    )

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert coins(paid, "a") == 14
    dear = next(s for s in paid.sponsors.sponsors if s.id == 1)
    assert dear.funds == pytest.approx(0.0)


def test_a_budget_not_spent_is_kept_for_the_next_payday() -> None:
    world = with_sponsors(sponsor(price=1.0, rate=1.0), crowd=seen(a=10))
    first = settle_due(replace(world, time=PAY_PERIOD))

    # 30 coins came in and 10 were spent, so 20 are left and 30 more come in: 50 coins for the 50
    # views since, where without the kept budget only 30 of them would be paid.
    second = settle_due(replace(replace(first, crowd=seen(a=60)), time=2 * PAY_PERIOD))

    assert first.sponsors.sponsors[0].funds == pytest.approx(20.0)
    assert coins(second, "a") - coins(first, "a") == 50


def test_a_budget_spent_to_the_last_coin_does_not_go_negative_from_float_rounding() -> None:
    # 59.743045295117305 and 30 coins come to 89.7430452951173, and 89.7430452951173 / 1.2 * 1.2
    # is above it.
    dear = sponsor(price=1.2, rate=1.0, funds=59.743045295117305)
    world = replace(
        with_sponsors(dear, crowd=seen(a=200)),
        sponsors=SponsorBook((dear,), topped_up_at=PAY_PERIOD),
    )

    paid = pay_sponsors(replace(world, time=2 * PAY_PERIOD))

    assert paid.sponsors.sponsors[0].funds == 0.0
    assert coins(paid, "a") == 90


def test_a_budget_stops_filling_at_its_cap() -> None:
    quiet = 100 * PAY_PERIOD
    world = with_sponsors(sponsor(price=1.0, rate=1.0), crowd=seen(a=10_000), time=quiet)

    paid = pay_sponsors(replace(world, time=quiet))

    assert coins(paid, "a") == BUDGET_CAP_SECONDS


def test_nothing_is_paid_within_the_period_of_the_last_payday_or_without_sponsors_or_views() -> (
    None
):
    world = with_sponsors(sponsor(), crowd=seen(a=10))
    paid = pay_sponsors(replace(world, time=PAY_PERIOD))
    nobody = replace(world, sponsors=SponsorBook(), time=PAY_PERIOD)
    nothing_seen = with_sponsors(sponsor(), time=PAY_PERIOD)

    again = pay_sponsors(replace(paid, crowd=seen(a=20), time=PAY_PERIOD + 29.0))

    assert pay_sponsors(replace(world, time=PAY_PERIOD - 0.1)) == replace(
        world, time=PAY_PERIOD - 0.1
    )
    assert again == replace(paid, crowd=seen(a=20), time=PAY_PERIOD + 29.0)
    assert pay_sponsors(nobody) == nobody
    assert pay_sponsors(nothing_seen) == nothing_seen


def test_the_clock_waits_for_a_payday_only_when_there_are_views_and_sponsors_to_pay_them() -> None:
    world = with_sponsors(sponsor(), crowd=seen(a=10), time=17.0)
    paid_at_30 = replace(world, time=47.3, sponsors=SponsorBook((sponsor(),), topped_up_at=30.0))

    assert next_payday(world) == PAY_PERIOD
    assert next_due(world) == PAY_PERIOD
    assert next_payday(paid_at_30) == 2 * PAY_PERIOD
    assert next_payday(replace(world, sponsors=SponsorBook())) is None
    assert next_payday(replace(world, crowd=Crowd())) is None


def test_a_payday_the_clock_arrives_at_late_is_still_held_once_and_not_skipped() -> None:
    world = with_sponsors(sponsor(price=1.0), crowd=seen(a=10))

    late = settle_due(replace(world, time=PAY_PERIOD + 0.003))
    much_later = settle_due(replace(world, time=3 * PAY_PERIOD + 12.0))

    assert coins(late, "a") == 10 and late.sponsors.topped_up_at == PAY_PERIOD
    assert coins(much_later, "a") == 10
    assert much_later.sponsors.topped_up_at == 3 * PAY_PERIOD
    assert next_payday(late) is None


def test_views_seen_after_a_quiet_payday_are_paid_at_once_not_a_period_later() -> None:
    # Nobody passed a slot at 30, so nothing was paid; the first views come at 47.3.
    world = with_sponsors(sponsor(price=1.0), crowd=Crowd(), time=47.3)
    seen_now = replace(world, crowd=seen(a=4))

    assert next_payday(seen_now) == 47.3
    assert coins(settle_due(seen_now), "a") == 4


@pytest.mark.parametrize(
    ("time", "payday"),
    [(0.0, 0.0), (0.1, 0.0), (29.9, 0.0), (29.9999999999, 30.0), (30.0, 30.0), (47.3, 30.0)],
)
def test_the_latest_payday_is_the_last_multiple_of_the_period_not_after_the_time(
    time: float, payday: float
) -> None:
    assert latest_payday(time) == payday


def test_views_that_come_in_between_paydays_are_paid_at_the_next_multiple_not_straight_away() -> (
    None
):
    world = with_sponsors(
        sponsor(),
        crowd=replace(new_crowd(40, seed=1), views=seen(a=5).views),
    )

    # The visitors' steps wake the clock at times that are no paydays, and the sponsors wait.
    mid = settle_due(replace(world, time=PAY_PERIOD + 7.3))

    assert all(t.time % PAY_PERIOD == 0 for t in mid.ledger.transfers if t.channel == "sponsors")
    assert mid.ledger.transfers[0].time == PAY_PERIOD


def test_the_default_sponsors_bid_different_prices_and_put_in_ten_coins_a_second() -> None:
    book = default_sponsors()

    assert len({s.price for s in book.sponsors}) == len(book.sponsors) == 3
    assert sum(s.rate for s in book.sponsors) == 10.0
    assert all(s.funds == 0 for s in book.sponsors)


def test_visitors_who_see_a_slot_earn_its_owner_coins_from_the_sponsors() -> None:
    # One slot in the middle showing content, a crowd that comes in and walks to it.
    slot = Facility("ad", 0.0, (10_000.0,) * 3)
    world = replace(
        WORLD,
        parcels=(Parcel(7, 7, "a", slot),),
        crowd=new_crowd(60, seed=2),
        sponsors=default_sponsors(),
    )

    after = settle_due(replace(world, time=300.0))

    assert coins(after, "a") > 0
    assert coins(after, "b") == 0
    assert {t.channel for t in after.ledger.transfers} == {"sponsors"}
    assert after.crowd.viewed("L7-7") > 0
