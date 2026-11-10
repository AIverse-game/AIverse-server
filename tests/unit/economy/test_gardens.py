"""The garden: a one-parcel facility that pays its owner once and raises every income of its
block by a tenth, while it draws a few visitors."""

from dataclasses import replace

import pytest

from aiverse_server.agents.observation import observe
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.admissions import AdmissionBook
from aiverse_server.core.compute import ComputeBook, Permit
from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Facility, Link, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.sponsors import Sponsor, SponsorBook
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy import visitors
from aiverse_server.economy.admissions import FEES, due_fees, pay_admissions
from aiverse_server.economy.compute import (
    AD_WORKLOAD,
    GARDEN_WORKLOAD,
    MALL_WORKLOAD,
    NODE_RATE,
    PORTAL_WORKLOAD,
    SELF_RATE,
    VENUE_WORKLOAD,
    garden_room,
    on_demand_cost,
)
from aiverse_server.economy.gardens import (
    GARDEN_REWARD,
    block_has_garden,
    garden_planned,
)
from aiverse_server.economy.land import claimed
from aiverse_server.economy.sponsors import PAY_PERIOD, pay_sponsors
from aiverse_server.economy.visitors import GARDEN_PIECES, MALL_PIECES, WORKSPACE_PIECES
from aiverse_server.game.actions import (
    ActionRejected,
    BuildAd,
    BuildGarden,
    BuildVenue,
    Teleport,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.server.app import Game

GARDEN = Facility("garden", 0.0)
AD = Facility("ad", 0.0, (10_000.0,))
WORKSPACE = Facility("workspace", 0.0)
BLOCK_A = tuple((col, row) for row in range(3) for col in range(3))
BLOCK_B = tuple((col + 3, row) for row in range(3) for col in range(3))

# 90 x 90 in blocks of 3 x 3 parcels. a owns the block of L0-0 to L2-2, and b the block of L3-0
# to L5-2; c holds no land.
MAP = WorldMap(size=90, block_cells=3, venue_cells=2)


def resident(resident_id: str, coins: int = 5_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(0, 0), coins=coins)


def land(**facilities: Facility) -> tuple[Parcel, ...]:
    """The two blocks, with `facilities` on the parcels named by keyword (`L1_1=...`)."""
    on = {name.replace("_", "-"): facility for name, facility in facilities.items()}
    return tuple(
        Parcel(col, row, owner, on.get(f"L{col}-{row}"))
        for owner, cells in (("a", BLOCK_A), ("b", BLOCK_B))
        for col, row in cells
    )


def world(**facilities: Facility) -> World:
    return World(
        map=MAP,
        residents=(resident("a"), resident("b"), resident("c")),
        parcels=land(**facilities),
    )


def at(w: World, time: float) -> World:
    return settle_due(replace(w, time=time))


def coins(w: World, resident_id: str) -> int:
    return coin_balance(w, resident_id)


def paid(w: World, channel: str) -> list[tuple[str, str, int]]:
    return [(t.payer, t.payee, t.amount) for t in w.ledger.transfers if t.channel == channel]


# Building


def test_a_garden_is_the_cheapest_facility_to_build() -> None:
    assert GARDEN_WORKLOAD < min(AD_WORKLOAD, PORTAL_WORKLOAD, MALL_WORKLOAD, VENUE_WORKLOAD)


def test_a_garden_appears_when_its_job_ends_and_not_before() -> None:
    building = perform(world(), BuildGarden("a", "L1-1", "self"))
    ends = GARDEN_WORKLOAD / SELF_RATE

    before = at(building, ends - 1)
    after = at(building, ends)

    assert next(p for p in before.parcels if p.id == "L1-1").facility is None
    assert next(p for p in after.parcels if p.id == "L1-1").facility == Facility("garden", ends)


def test_the_town_pays_the_parcels_owner_a_reward_once_when_the_garden_is_finished() -> None:
    building = perform(world(), BuildGarden("a", "L1-1", "self"))

    before = at(building, GARDEN_WORKLOAD / SELF_RATE - 1)
    built = at(building, GARDEN_WORKLOAD / SELF_RATE)
    later = at(built, 10_000)

    assert coins(before, "a") == 5_000
    assert coins(built, "a") == 5_000 + GARDEN_REWARD
    assert coins(later, "a") == 5_000 + GARDEN_REWARD
    assert paid(later, "garden_reward") == [(SYSTEM, "a", GARDEN_REWARD)]


def test_hiring_a_node_for_a_garden_costs_coins_and_the_reward_pays_back_some() -> None:
    building = perform(world(), BuildGarden("a", "L1-1", "on_demand"))

    built = at(building, GARDEN_WORKLOAD / NODE_RATE)

    assert coins(built, "a") == 5_000 - on_demand_cost(GARDEN_WORKLOAD) + GARDEN_REWARD


def test_only_the_parcels_owner_builds_a_garden_not_someone_holding_shares_in_it() -> None:
    shared = replace(
        world(),
        parcels=tuple(
            replace(p, shares=(("a", 50), ("c", 50))) if p.id == "L1-1" else p
            for p in world().parcels
        ),
    )

    with pytest.raises(ActionRejected) as raised:
        perform(shared, BuildGarden("c", "L1-1", "self"))

    assert raised.value.code == "not_your_parcel"


def test_a_garden_is_refused_on_a_parcel_that_has_a_facility_or_belongs_to_another() -> None:
    with pytest.raises(ActionRejected) as built:
        perform(world(L1_1=AD), BuildGarden("a", "L1-1", "self"))
    with pytest.raises(ActionRejected) as others:
        perform(world(), BuildGarden("a", "L4-1", "self"))

    assert (built.value.code, others.value.code) == ("parcel_built", "not_your_parcel")


def test_a_block_has_one_garden_whether_it_is_built_or_still_being_built() -> None:
    building = perform(world(), BuildGarden("a", "L1-1", "self"))
    with pytest.raises(ActionRejected) as planned:
        perform(building, BuildGarden("a", "L2-2", "self"))
    with pytest.raises(ActionRejected) as standing:
        perform(world(L0_0=GARDEN), BuildGarden("a", "L2-2", "self"))

    assert (planned.value.code, standing.value.code) == ("garden_exists", "garden_exists")


def test_another_block_may_have_its_own_garden() -> None:
    building = perform(world(L1_1=GARDEN), BuildGarden("b", "L4-1", "self"))

    assert [job.parcel_id for job in building.compute.jobs] == ["L4-1"]


def test_a_garden_cannot_be_built_on_land_a_venue_takes_up() -> None:
    building = perform(world(), BuildVenue("a", "L0-0", "workspace", "self"))

    with pytest.raises(ActionRejected) as raised:
        perform(building, BuildGarden("a", "L1-1", "self"))

    assert raised.value.code == "parcel_covered"


def test_a_garden_cannot_be_built_where_the_owner_promised_a_builder_an_ad_slot() -> None:
    promised = replace(
        world(),
        compute=ComputeBook(permits=(Permit("L1-1", "ad", "a", "c", 0, 300.0),)),
    )

    with pytest.raises(ActionRejected) as raised:
        perform(promised, BuildGarden("a", "L1-1", "self"))

    assert raised.value.code == "parcel_permitted"


def test_a_garden_that_loses_the_parcel_to_a_faster_ad_slot_earns_no_reward() -> None:
    building = perform(world(), BuildGarden("a", "L1-1", "self"))
    building = perform(building, BuildAd("a", "L1-1", "on_demand"))

    done = at(building, GARDEN_WORKLOAD / SELF_RATE)

    assert next(p for p in done.parcels if p.id == "L1-1").facility is not None
    assert next(p for p in done.parcels if p.id == "L1-1").facility.kind == "ad"  # type: ignore[union-attr]
    assert done.compute.jobs == ()
    assert paid(done, "garden_reward") == []


def test_a_garden_that_beats_a_slower_ad_slot_to_the_parcel_is_rewarded_once() -> None:
    building = perform(world(), BuildAd("a", "L1-1", "self"))
    building = perform(building, BuildGarden("a", "L1-1", "on_demand"))

    done = at(building, AD_WORKLOAD / SELF_RATE)

    assert next(p for p in done.parcels if p.id == "L1-1").facility == Facility("garden", 10.0)
    assert done.compute.jobs == ()
    assert paid(done, "garden_reward") == [(SYSTEM, "a", GARDEN_REWARD)]


def test_garden_room_says_where_a_garden_could_start() -> None:
    plain = world()
    taken = claimed(plain)
    parcels = {p.id: p for p in plain.parcels}
    walled = world(L0_0=WORKSPACE)

    free = garden_room(plain, parcels["L1-1"], taken)
    built_on = garden_room(
        walled, next(p for p in walled.parcels if p.id == "L0-0"), claimed(walled)
    )
    with_garden = world(L2_2=GARDEN)
    blocked = garden_room(
        with_garden,
        next(p for p in with_garden.parcels if p.id == "L1-1"),
        claimed(with_garden),
    )

    assert (free, built_on, blocked) == (True, False, False)


def test_a_garden_being_built_is_planned_but_does_not_yet_count_as_a_garden() -> None:
    building = perform(world(), BuildGarden("a", "L1-1", "self"))

    assert garden_planned(building, "L0-0") and not block_has_garden(building, "L0-0")
    assert not garden_planned(building, "L4-1")
    assert block_has_garden(world(L2_2=GARDEN), "L0-0")


# The bonus on sponsors' pay


def sponsored(views: float, **facilities: Facility) -> World:
    sponsors = SponsorBook((Sponsor(0, "s", 2.0, 1_000.0),))
    return replace(
        world(**facilities),
        sponsors=sponsors,
        crowd=Crowd(views=(("L1-0", views), ("L4-0", views))),
        time=PAY_PERIOD,
    )


def test_ad_slots_in_a_block_with_a_garden_are_paid_a_tenth_more_by_sponsors() -> None:
    world_ = sponsored(50, L1_0=AD, L4_0=AD, L2_2=GARDEN)

    done = pay_sponsors(world_)

    # 50 views at 2 coins: 100 for a slot, 110 in a block with a garden.
    assert (coins(done, "a") - 5_000, coins(done, "b") - 5_000) == (110, 100)
    assert paid(done, "sponsors") == [(SYSTEM, "a", 100), (SYSTEM, "b", 100)]
    assert paid(done, "garden_bonus") == [(SYSTEM, "a", 10)]
    assert done.sponsors.last_paid_to("L1-0") == 110


def test_the_bonus_follows_the_parcels_shares() -> None:
    world_ = sponsored(50, L1_0=AD, L4_0=AD, L2_2=GARDEN)
    shared = replace(
        world_,
        parcels=tuple(
            replace(p, shares=(("a", 50), ("c", 50))) if p.col < 3 else p for p in world_.parcels
        ),
    )

    done = pay_sponsors(shared)

    assert [(payee, amount) for _, payee, amount in paid(done, "garden_bonus")] == [
        ("a", 5),
        ("c", 5),
    ]


def test_the_bonus_is_rounded_on_the_whole_income_so_it_does_not_vanish_on_small_sums() -> None:
    # 2.3 coins of pay is 2 coins, and 2.53 with a garden is 3: the garden adds one coin where
    # a tenth of the rounded 2 would add nothing.
    done = pay_sponsors(sponsored(1.15, L1_0=AD, L4_0=AD, L2_2=GARDEN))

    assert paid(done, "sponsors")[0] == (SYSTEM, "a", 2)
    assert paid(done, "garden_bonus") == [(SYSTEM, "a", 1)]


def test_a_garden_still_being_built_adds_nothing() -> None:
    building = perform(sponsored(50, L1_0=AD, L4_0=AD), BuildGarden("a", "L2-2", "self"))

    done = pay_sponsors(building)

    assert paid(done, "garden_bonus") == []


# The bonus on admission fees


def owed(amount: float, **facilities: Facility) -> World:
    book = AdmissionBook(unpaid=(("L1-0", amount), ("L4-0", amount)))
    return replace(world(**facilities), admissions=book, time=PAY_PERIOD)


def test_fees_in_a_block_with_a_garden_are_paid_a_tenth_more() -> None:
    world_ = owed(100.0, L1_0=WORKSPACE, L4_0=WORKSPACE, L2_2=GARDEN)

    done = pay_admissions(world_)

    assert due_fees(world_) == {"L1-0": 110, "L4-0": 100}
    assert (coins(done, "a") - 5_000, coins(done, "b") - 5_000) == (110, 100)
    assert paid(done, "visitors") == [(SYSTEM, "a", 100), (SYSTEM, "b", 100)]
    assert paid(done, "garden_bonus") == [(SYSTEM, "a", 10)]
    assert done.admissions.last_paid_to("L1-0") == 110
    assert done.admissions.unpaid == ()


def test_part_of_a_coin_of_fees_is_carried_and_the_tenth_is_not_counted_twice() -> None:
    world_ = owed(10.5, L1_0=WORKSPACE, L4_0=WORKSPACE, L2_2=GARDEN)

    done = pay_admissions(world_)

    # 10.5 coins of fees are 11.55 with the garden: 11 are paid, 10 for the fees and 1 for the
    # garden, and what is left, the 0.55 over 1.1, is 0.5 coins of fees.
    assert (coins(done, "a") - 5_000, coins(done, "b") - 5_000) == (11, 10)
    assert dict(done.admissions.unpaid) == {
        "L1-0": pytest.approx(0.5),
        "L4-0": pytest.approx(0.5),
    }


def test_a_garden_earns_its_own_fee_too_and_the_bonus_applies_to_it() -> None:
    world_ = owed(100.0, L1_0=GARDEN, L4_0=GARDEN)

    assert due_fees(world_) == {"L1-0": 110, "L4-0": 110}


# Tolls are not raised

# A toll moves coins between residents. A bonus from the town on it could be farmed by teleporting
# back and forth between portals of one's own, so only money from outside the town is raised.


def toll_world(**facilities: Facility) -> World:
    portal = Facility("portal", 0.0)
    return replace(
        world(L1_1=portal, L4_1=portal, **facilities),
        residents=(
            replace(resident("c"), position=Position(15, 15)),
            resident("a"),
            resident("b"),
        ),
        links=(Link("L1-1", "L4-1", 100),),
    )


def test_a_toll_paid_into_a_block_with_a_garden_is_not_raised() -> None:
    done = perform(toll_world(L2_2=GARDEN), Teleport("c", "L4-1"))

    assert (coins(done, "a") - 5_000, coins(done, "b") - 5_000) == (50, 50)
    assert paid(done, "garden_bonus") == []


def test_teleporting_between_ones_own_portals_in_a_garden_block_makes_no_coins() -> None:
    portal = Facility("portal", 0.0)
    own = replace(
        world(L1_1=portal, L2_1=portal, L2_2=GARDEN),
        residents=(replace(resident("a"), position=Position(15, 15)), resident("b"), resident("c")),
        links=(Link("L1-1", "L2-1", 1_000),),
    )

    there = perform(own, Teleport("a", "L2-1"))
    back = perform(there, Teleport("a", "L1-1"))

    assert coins(back, "a") == 5_000


# Visitors


@pytest.fixture
def no_working_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visitors, "ROUTE_SHARE", 0.0)


def test_a_garden_draws_fewer_visitors_than_a_workspace_and_more_than_a_mall() -> None:
    assert MALL_PIECES < GARDEN_PIECES < WORKSPACE_PIECES
    assert FEES["mall"] < FEES["garden"] < FEES["workspace"] < FEES["game_hall"]


@pytest.mark.usefixtures("no_working_route")
def test_a_tourist_goes_into_a_garden_and_pays_its_fee() -> None:
    entrance = Position(50, 50)
    lone = World(
        map=WorldMap(size=100),
        residents=(resident("a"),),
        parcels=(Parcel(5, 3, "a", GARDEN),),
        crowd=Crowd(seed=0, visitors=(Visitor(0, "away", 5.0, entrance, entrance),)),
    )
    walking = at(lone, 5.0).crowd.visitors[0]

    inside = at(at(lone, 5.0), walking.until)

    assert (walking.goal, walking.stop) == ("place", "L5-3")
    assert dict(inside.admissions.unpaid)["L5-3"] > 0


# What an agent sees and does


def test_an_agent_builds_a_garden_and_sees_where_one_could_go() -> None:
    game = Game(world(), 600, lambda: 0.0)

    before = observe(game.world, "a", game.begin_turn("a"), game.remaining())
    built = ResidentTools(game, "a").build_garden("L1-1", "self")
    after = observe(game.world, "a", game.begin_turn("a"), game.remaining())
    rooms = {p.id: p.garden_room for p in before.land.mine}

    assert rooms["L1-1"] and rooms["L0-0"]
    assert (built.ok, built.message) == (True, "job 1: garden on L1-1 ready in 200 s")
    assert not any(p.garden_room for p in after.land.mine)
    assert [option.source for option in before.compute.garden] == [
        "self",
        "on_demand",
        "subscription",
    ]
