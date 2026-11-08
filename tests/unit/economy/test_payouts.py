"""Payouts: what a parcel's facility was paid at its latest payday, as pages are told."""

from dataclasses import replace

from aiverse_server.core.admissions import AdmissionBook
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.sponsors import Sponsor, SponsorBook
from aiverse_server.core.visitors import Crowd
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.payouts import Payout, latest_payout
from aiverse_server.economy.sponsors import PAY_PERIOD
from aiverse_server.game.clock import settle_due

# a owns an ad slot on L7-7 and a workspace on L7-8, each paid at the first payday: the slot
# for 10 people at 2 coins each, the workspace for 4 coins of fees; the slot on L7-9 has no views.
WORLD = World(
    map=WorldMap(size=150),
    residents=(Resident("a", "a", "#123456", Position(0, 0), coins=1_000),),
    parcels=(
        Parcel(7, 7, "a", Facility("ad", 0.0, (10_000.0,))),
        Parcel(7, 8, "a", Facility("workspace", 0.0)),
        Parcel(7, 9, "a", Facility("ad", 0.0, (10_000.0,))),
    ),
    sponsors=SponsorBook(sponsors=(Sponsor(0, "s", price=2.0, rate=1_000.0),)),
    crowd=Crowd(views=(("L7-7", 10.0),)),
    admissions=AdmissionBook(unpaid=(("L7-8", 4.0),)),
)


def test_a_facility_that_was_never_paid_has_no_payout() -> None:
    assert latest_payout(WORLD, "L7-7") is None


def test_a_slot_paid_by_sponsors_reports_the_payday_and_its_coins() -> None:
    paid = settle_due(replace(WORLD, time=PAY_PERIOD))

    assert latest_payout(paid, "L7-7") == Payout(PAY_PERIOD, 20)


def test_a_venue_paid_its_fees_reports_the_payday_and_its_coins() -> None:
    paid = settle_due(replace(WORLD, time=PAY_PERIOD))

    assert latest_payout(paid, "L7-8") == Payout(PAY_PERIOD, 4)


def test_a_facility_the_payday_did_not_pay_has_no_payout() -> None:
    paid = settle_due(replace(WORLD, time=PAY_PERIOD))

    assert latest_payout(paid, "L7-9") is None


def test_both_books_paying_the_same_parcel_at_one_payday_are_added() -> None:
    world = replace(
        WORLD,
        sponsors=SponsorBook(topped_up_at=60.0, last_paid=(("L7-7", 20),)),
        admissions=AdmissionBook(paid_at=60.0, last_paid=(("L7-7", 5),)),
    )

    assert latest_payout(world, "L7-7") == Payout(60.0, 25)


def test_the_later_payday_wins_when_the_books_last_paid_the_parcel_at_different_ones() -> None:
    world = replace(
        WORLD,
        sponsors=SponsorBook(topped_up_at=30.0, last_paid=(("L7-7", 20),)),
        admissions=AdmissionBook(paid_at=60.0, last_paid=(("L7-7", 5),)),
    )

    assert latest_payout(world, "L7-7") == Payout(60.0, 5)
