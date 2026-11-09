from dataclasses import replace

import pytest

from aiverse_server.core.compute import Permit
from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.commissions import MAX_FEE
from aiverse_server.economy.compute import AD_WORKLOAD, NODE_RATE, PERMIT_SECONDS, SELF_RATE
from aiverse_server.game.actions import (
    ActionRejected,
    BuildAd,
    BuildVenue,
    LeaveGame,
    OfferBuildCommission,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game, total_coins

FEE = 400
NODE_SECONDS_FOR_AD = AD_WORKLOAD / NODE_RATE
SELF_SECONDS_FOR_AD = AD_WORKLOAD / SELF_RATE


def resident(resident_id: str, coins: int = 5_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(5, 5), coins=coins)


# o owns two bare parcels and has no compute worth the name; b builds for coins and owns a
# parcel too, c is a bystander.
WORLD = World(
    map=WorldMap(size=100, venue_cells=1),
    residents=(resident("o"), resident("b"), resident("c")),
    parcels=(Parcel(5, 3, "o"), Parcel(6, 3, "o"), Parcel(1, 1, "b")),
)


def ask_builder(world: World, parcel: str = "L5-3", kind: str = "ad", fee: int = FEE) -> World:
    """o, the owner, asks b to build on their parcel."""
    return perform(world, OfferBuildCommission("o", "b", parcel, kind, fee, True))  # type: ignore[arg-type]


def ask_owner(world: World, parcel: str = "L5-3", kind: str = "ad", fee: int = FEE) -> World:
    """b, the builder, asks o for leave to build on their parcel."""
    return perform(world, OfferBuildCommission("b", "o", parcel, kind, fee, False))  # type: ignore[arg-type]


def agreed(world: World = WORLD, parcel: str = "L5-3", kind: str = "ad") -> World:
    """o and b agreed that b builds `kind` on `parcel` for FEE."""
    return perform(ask_builder(world, parcel, kind), RespondToEvent("b", 1, True))


def facility_on(world: World, parcel_id: str) -> str | None:
    found = next(p.facility for p in world.parcels if p.id == parcel_id)
    return found.kind if found is not None else None


def test_nothing_may_be_built_on_a_parcel_that_is_not_yours_without_the_owners_agreement() -> None:
    asked = ask_owner(WORLD)

    with pytest.raises(ActionRejected) as before:
        perform(WORLD, BuildAd("b", "L5-3", "self"))
    with pytest.raises(ActionRejected) as waiting:
        perform(asked, BuildAd("b", "L5-3", "self"))

    assert (before.value.code, waiting.value.code) == ("not_your_parcel", "not_your_parcel")


def test_an_owner_who_asks_sets_the_fee_aside_at_once_and_a_builder_who_asks_does_not() -> None:
    by_owner = ask_builder(WORLD)
    by_builder = ask_owner(WORLD)

    assert coin_balance(by_owner, "o") == 5_000 - FEE
    assert total_coins(by_owner, "o") == 5_000
    assert coin_balance(by_builder, "o") == 5_000


def test_the_builder_builds_on_the_owners_parcel_once_the_owner_has_agreed() -> None:
    world = agreed()

    started = perform(world, BuildAd("b", "L5-3", "self"))

    assert [job.parcel_id for job in started.compute.jobs] == ["L5-3"]
    assert started.compute.jobs[0].owner_id == "b"
    assert started.compute.permits == ()


def test_the_builder_is_paid_the_fee_when_the_facility_is_finished_and_not_before() -> None:
    started = perform(agreed(), BuildAd("b", "L5-3", "self"))

    before = settle_due(replace(started, time=SELF_SECONDS_FOR_AD - 0.1))
    done = settle_due(replace(started, time=SELF_SECONDS_FOR_AD))

    assert (coin_balance(before, "b"), total_coins(before, "o")) == (5_000, 5_000)
    assert coin_balance(done, "b") == 5_000 + FEE
    assert coin_balance(done, "o") == 5_000 - FEE
    assert facility_on(done, "L5-3") == "ad"
    assert next(p.owner_id for p in done.parcels if p.id == "L5-3") == "o"
    last = done.ledger.transfers[-1]
    assert (last.payer, last.payee, last.amount, last.channel) == ("o", "b", FEE, "commission")


def test_a_builder_who_asks_is_paid_by_the_owner_who_agrees() -> None:
    world = perform(ask_owner(WORLD), RespondToEvent("o", 1, True))

    started = perform(world, BuildAd("b", "L5-3", "on_demand"))
    done = settle_due(replace(started, time=NODE_SECONDS_FOR_AD))

    assert coin_balance(world, "o") == 5_000 - FEE
    assert coin_balance(done, "o") == 5_000 - FEE
    assert coin_balance(done, "b") > 5_000 + FEE - AD_WORKLOAD * 100


def test_rejecting_or_withdrawing_gives_the_owners_fee_back() -> None:
    rejected = perform(ask_builder(WORLD), RespondToEvent("b", 1, False))
    withdrawn = perform(ask_builder(WORLD), WithdrawEvent("o", 1))

    assert coin_balance(rejected, "o") == 5_000
    assert coin_balance(withdrawn, "o") == 5_000
    assert rejected.compute.permits == withdrawn.compute.permits == ()


def test_a_builder_who_asks_cannot_be_accepted_by_an_owner_who_cannot_pay() -> None:
    poor = replace(WORLD, residents=(resident("o", coins=FEE - 1), resident("b"), resident("c")))
    asked = ask_owner(poor)

    with pytest.raises(ActionRejected) as rejected:
        perform(asked, RespondToEvent("o", 1, True))

    assert rejected.value.code == "not_enough_coins"


def test_the_permit_is_for_that_parcel_and_that_kind_of_facility_only() -> None:
    world = agreed(kind="workspace")

    with pytest.raises(ActionRejected) as other_parcel:
        perform(world, BuildAd("b", "L6-3", "self"))
    with pytest.raises(ActionRejected) as other_kind:
        perform(world, BuildAd("b", "L5-3", "self"))
    with pytest.raises(ActionRejected) as bystander:
        perform(world, BuildVenue("c", "L5-3", "workspace", "self"))
    built = perform(world, BuildVenue("b", "L5-3", "workspace", "self"))

    assert (other_parcel.value.code, other_kind.value.code, bystander.value.code) == (
        "not_your_parcel",
        "not_your_parcel",
        "not_your_parcel",
    )
    assert built.compute.jobs[0].kind == "build_workspace"


def test_an_unused_permit_lapses_and_the_fee_goes_back_to_the_owner() -> None:
    world = agreed()

    lapsed = settle_due(replace(world, time=PERMIT_SECONDS))

    with pytest.raises(ActionRejected) as late:
        perform(lapsed, BuildAd("b", "L5-3", "self"))
    assert lapsed.compute.permits == ()
    assert coin_balance(lapsed, "o") == 5_000
    assert late.value.code == "not_your_parcel"


def test_a_permit_still_holds_just_before_it_lapses() -> None:
    world = agreed()

    almost = settle_due(replace(world, time=PERMIT_SECONDS - 1.0))

    assert len(almost.compute.permits) == 1
    assert perform(almost, BuildAd("b", "L5-3", "self")).compute.jobs[0].fee == FEE


def test_a_promised_parcel_is_not_built_on_by_anyone_else_meanwhile() -> None:
    world = agreed()

    with pytest.raises(ActionRejected) as by_owner:
        perform(world, BuildAd("o", "L5-3", "self"))
    with pytest.raises(ActionRejected) as promised_again:
        ask_builder(world)

    assert (by_owner.value.code, promised_again.value.code) == ("parcel_permitted",) * 2


def test_the_owner_may_still_build_on_their_other_parcels() -> None:
    world = agreed()

    built = perform(world, BuildAd("o", "L6-3", "self"))

    assert [job.parcel_id for job in built.compute.jobs] == ["L6-3"]


def test_the_builder_leaving_hands_the_fee_back_and_builds_nothing() -> None:
    started = perform(agreed(), BuildAd("b", "L5-3", "self"))

    left = perform(started, LeaveGame("b"))
    later = settle_due(replace(left, time=SELF_SECONDS_FOR_AD))

    assert coin_balance(later, "o") == 5_000
    assert facility_on(later, "L5-3") is None


def test_the_builder_leaving_before_starting_also_hands_the_fee_back() -> None:
    left = perform(agreed(), LeaveGame("b"))

    assert left.compute.permits == ()
    assert coin_balance(left, "o") == 5_000


def test_the_owner_leaving_hands_their_fee_back_into_their_balance() -> None:
    left = perform(agreed(), LeaveGame("o"))

    assert left.compute.permits == ()
    assert total_coins(left, "o") == 5_000


def test_the_end_of_the_game_hands_every_fee_back() -> None:
    started = perform(agreed(), BuildAd("b", "L5-3", "self"))

    ended = end_game(started)

    assert coin_balance(ended, "o") == 5_000
    assert coin_balance(ended, "b") == 5_000


def test_the_ranking_counts_the_fee_set_aside_in_every_stage() -> None:
    proposed = ask_builder(WORLD)
    permitted = agreed()
    started = perform(permitted, BuildAd("b", "L5-3", "self"))

    totals = [total_coins(world, "o") for world in (proposed, permitted, started)]

    assert totals == [5_000] * 3


@pytest.mark.parametrize(
    ("parcel", "kind", "fee", "code"),
    [
        ("L1-1", "ad", FEE, "not_your_parcel"),
        ("L9-9", "ad", FEE, "unknown_parcel"),
        ("L5-3", "portal", FEE, "invalid_commission"),
        ("L5-3", "ad", -1, "invalid_fee"),
        ("L5-3", "ad", MAX_FEE + 1, "invalid_fee"),
        ("L5-3", "ad", True, "invalid_fee"),
    ],
)
def test_a_commission_must_name_the_owners_parcel_a_buildable_kind_and_a_sane_fee(
    parcel: str, kind: str, fee: int, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        ask_builder(WORLD, parcel, kind, fee)

    assert rejected.value.code == code


def test_a_parcel_that_is_already_built_on_cannot_be_commissioned() -> None:
    built = settle_due(replace(perform(WORLD, BuildAd("o", "L5-3", "on_demand")), time=60.0))

    with pytest.raises(ActionRejected) as rejected:
        ask_builder(built)

    assert rejected.value.code == "parcel_built"


def test_the_builder_cannot_be_the_owner_of_the_parcel_they_ask_for() -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, OfferBuildCommission("o", "o", "L5-3", "ad", FEE, True))

    assert rejected.value.code == "self_addressed"


def test_a_free_commission_is_allowed() -> None:
    world = agreed_free()

    done = settle_due(
        replace(perform(world, BuildAd("b", "L5-3", "self")), time=SELF_SECONDS_FOR_AD)
    )

    assert facility_on(done, "L5-3") == "ad"
    assert (coin_balance(done, "o"), coin_balance(done, "b")) == (5_000, 5_000)


def agreed_free() -> World:
    return perform(ask_builder(WORLD, fee=0), RespondToEvent("b", 1, True))


def test_only_so_many_commissions_wait_between_the_same_two() -> None:
    world = WORLD
    for col in (5, 6):
        world = ask_builder(world, f"L{col}-3")

    third = replace(world, parcels=(*world.parcels, Parcel(7, 3, "o")))
    fourth = replace(third, parcels=(*third.parcels, Parcel(8, 3, "o")))
    third = ask_builder(third, "L7-3")
    with pytest.raises(ActionRejected) as rejected:
        ask_builder(replace(third, parcels=fourth.parcels), "L8-3")

    assert rejected.value.code == "too_many_commissions"


def test_a_permit_is_a_plain_record_and_cannot_pay_nobody() -> None:
    with pytest.raises(ValueError, match="between one resident and themselves"):
        Permit("L5-3", "ad", "o", "o", 10, 100.0)


# A 2 x 2 venue square: o owns L4-4..L5-5, so a venue on L4-4 takes up all four parcels.
SQUARE = replace(
    WORLD,
    map=WorldMap(size=100, block_cells=2, venue_cells=2),
    parcels=(Parcel(4, 4, "o"), Parcel(5, 4, "o"), Parcel(4, 5, "o"), Parcel(5, 5, "o")),
)


def test_the_owner_cannot_cover_a_promised_parcel_with_a_venue_of_their_own() -> None:
    world = perform(ask_builder(SQUARE, "L5-5"), RespondToEvent("b", 1, True))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuildVenue("o", "L4-4", "workspace", "self"))

    assert rejected.value.code == "parcel_permitted"


def test_the_owner_cannot_build_in_the_square_of_a_venue_promised_to_a_builder() -> None:
    world = perform(ask_builder(SQUARE, "L4-4", "workspace"), RespondToEvent("b", 1, True))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuildAd("o", "L5-5", "self"))

    assert rejected.value.code == "parcel_permitted"


def test_a_commission_inside_the_square_of_a_promised_venue_is_refused() -> None:
    world = perform(ask_builder(SQUARE, "L4-4", "workspace"), RespondToEvent("b", 1, True))

    with pytest.raises(ActionRejected) as rejected:
        ask_builder(world, "L5-5")

    assert rejected.value.code == "parcel_permitted"


def test_a_job_under_way_is_paid_to_its_builder_even_if_the_owner_has_left_meanwhile() -> None:
    started = perform(agreed(), BuildAd("b", "L5-3", "self"))

    left = perform(started, LeaveGame("o"))
    done = settle_due(replace(left, time=SELF_SECONDS_FOR_AD))

    assert facility_on(done, "L5-3") == "ad"
    assert coin_balance(done, "b") == 5_000 + FEE
    assert coin_balance(done, "o") == 5_000 - FEE
