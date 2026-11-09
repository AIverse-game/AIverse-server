"""Admission fees: taken at the door, paid out to the holders of the parcel at paydays."""

from dataclasses import replace

import pytest

from aiverse_server.core.admissions import AdmissionBook
from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy import visitors
from aiverse_server.economy.admissions import (
    FEES,
    admit,
    due_fees,
    next_admission_payday,
    pay_admissions,
)
from aiverse_server.economy.sponsors import PAY_PERIOD
from aiverse_server.economy.visitors import new_crowd
from aiverse_server.game.actions import LeaveGame, perform
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.lifecycle import total_coins

WORKSPACE = Facility("workspace", 0.0)
HALL = Facility("game_hall", 0.0, (10_000.0,))

# a owns the workspace on L7-7 and the hall on L7-8; b owns nothing; each has 1000 coins.
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        Resident("a", "a", "#123456", Position(0, 0), coins=1_000),
        Resident("b", "b", "#123456", Position(0, 0), coins=1_000),
    ),
    parcels=(Parcel(7, 7, "a", WORKSPACE), Parcel(7, 8, "a", HALL)),
)


def owed(workspace: float = 0.0, hall: float = 0.0, **kwargs: object) -> World:
    """The world with fees waiting at the two doors."""
    pairs = (("L7-7", workspace), ("L7-8", hall))
    book = AdmissionBook(unpaid=tuple((parcel, n) for parcel, n in pairs if n))
    return replace(WORLD, admissions=book, **kwargs)  # type: ignore[arg-type]


def coins(world: World, resident_id: str) -> int:
    return coin_balance(world, resident_id) - 1_000


def test_fees_are_paid_out_in_whole_coins_at_the_payday_and_the_rest_is_carried() -> None:
    world = owed(workspace=10.5, hall=3.0)

    before = settle_due(replace(world, time=PAY_PERIOD - 0.1))
    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert coins(before, "a") == 0
    assert coins(paid, "a") == 13
    assert dict(paid.admissions.unpaid) == {"L7-7": pytest.approx(0.5)}
    assert dict(paid.admissions.last_paid) == {"L7-7": 10, "L7-8": 3}
    assert all(
        (t.payer, t.payee, t.channel) == (SYSTEM, "a", "visitors") for t in paid.ledger.transfers
    )


def test_a_facility_owed_less_than_a_coin_is_not_paid_and_does_not_wake_the_clock() -> None:
    world = owed(workspace=0.9)

    assert due_fees(world) == {}
    assert next_admission_payday(world) is None
    assert next_due(world) is None
    assert settle_due(replace(world, time=10 * PAY_PERIOD)).ledger.transfers == ()


def test_the_carried_part_of_a_coin_is_paid_with_the_next_fees() -> None:
    world = owed(workspace=10.5)
    first = settle_due(replace(world, time=PAY_PERIOD))

    more = replace(first, admissions=admit(first.admissions, "L7-7", 1.0, FEES["workspace"]))
    second = settle_due(replace(more, time=2 * PAY_PERIOD))

    assert coins(second, "a") == 10 + 8 + 0  # 0.5 + 8 = 8.5 → 8, and 0.5 left again
    assert dict(second.admissions.unpaid) == {"L7-7": pytest.approx(0.5)}


def test_the_money_goes_to_the_holders_of_the_shares_in_proportion() -> None:
    shared = replace(WORLD.parcels[0], shares=(("a", 70), ("b", 30)))
    world = replace(owed(workspace=10.0), parcels=(shared, WORLD.parcels[1]))

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert (coins(paid, "a"), coins(paid, "b")) == (7, 3)


def test_an_owner_who_left_is_still_paid_for_people_who_went_in() -> None:
    world = perform(owed(workspace=10.0), LeaveGame("a"))

    paid = settle_due(replace(world, time=PAY_PERIOD))

    assert coins(paid, "a") == 10
    assert total_coins(paid, "a") == coin_balance(paid, "a")


def test_a_payday_the_clock_reaches_late_is_held_once_and_not_skipped() -> None:
    world = owed(workspace=10.0)

    late = settle_due(replace(world, time=PAY_PERIOD + 0.003))
    much_later = settle_due(replace(world, time=4 * PAY_PERIOD + 7.0))

    assert coins(late, "a") == 10 and late.admissions.paid_at == PAY_PERIOD
    assert coins(much_later, "a") == 10 and much_later.admissions.paid_at == 4 * PAY_PERIOD
    assert next_admission_payday(late) is None


def test_fees_taken_after_a_payday_wait_for_the_next_multiple() -> None:
    world = owed(workspace=10.0, time=PAY_PERIOD + 5.0)
    world = replace(world, admissions=replace(world.admissions, paid_at=PAY_PERIOD))

    assert next_admission_payday(world) == 2 * PAY_PERIOD
    assert pay_admissions(world) == world


def test_admitting_people_adds_the_fee_for_each_and_counts_them() -> None:
    book = admit(admit(AdmissionBook(), "L7-7", 2.0, 4), "L7-7", 1.5, 4)

    assert book.unpaid == (("L7-7", 14.0),)
    assert book.admitted_to("L7-7") == 3.5 and book.admitted_to("L9-9") == 0.0


def test_a_book_lists_each_parcel_once_in_order_with_amounts_that_make_sense() -> None:
    with pytest.raises(ValueError, match="once"):
        AdmissionBook(unpaid=(("L2-2", 1.0), ("L1-1", 1.0)))
    with pytest.raises(ValueError, match="positive"):
        AdmissionBook(admitted=(("L1-1", 0.0),))
    with pytest.raises(ValueError, match="integers"):
        AdmissionBook(last_paid=(("L1-1", 2.5),))  # type: ignore[arg-type]


def test_a_world_refuses_fees_for_a_parcel_nobody_owns() -> None:
    with pytest.raises(ValueError, match="nobody owns"):
        replace(WORLD, admissions=AdmissionBook(unpaid=(("L1-1", 1.0),)))


def test_working_tourists_who_go_in_earn_the_owner_coins_from_outside_the_town(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visitors, "ROUTE_SHARE", 1.0)
    world = replace(WORLD, crowd=new_crowd(60, seed=2))

    after = settle_due(replace(world, time=600.0))

    assert coins(after, "a") > 0
    assert coins(after, "b") == 0
    assert {t.channel for t in after.ledger.transfers} == {"visitors"}
    taken = sum(t.amount for t in after.ledger.transfers) + sum(
        coins for _, coins in after.admissions.unpaid
    )
    admitted = sum(people for _, people in after.admissions.admitted)
    assert 0 < taken <= admitted * max(FEES.values())
