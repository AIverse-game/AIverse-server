from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance, item_count
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.facilities import (
    AD_MATERIALS,
    FACILITY_SECONDS,
    ad_income,
    footfall,
    next_facility_cycle,
    visitors,
)
from aiverse_server.economy.studio import MATERIAL
from aiverse_server.game.actions import ActionRejected, BuildAd, LeaveGame, MoveTo, perform
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game


def resident(resident_id: str, x: float, y: float, materials: int = AD_MATERIALS) -> Resident:
    stacks = ((COMPUTE, 100), (MATERIAL, materials)) if materials else ((COMPUTE, 100),)
    return Resident(
        resident_id, resident_id, "#123456", Position(x, y), Inventory(stacks), coins=100
    )


# a stands in their own bare parcel L5-3, 15.8 units north of the market; b owns L6-3 next to it
# but stands far away in the south-east corner.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 55, 35), resident("b", 95, 95)),
    market=Market(Position(50, 50), 5),
    parcels=(Parcel(5, 3, "a"), Parcel(6, 3, "b")),
)

AD = Facility("ad", 0.0, FACILITY_SECONDS)


def with_ads(world: World, *owners: str) -> World:
    return replace(
        world,
        parcels=tuple(
            replace(parcel, facility=AD) if parcel.owner_id in owners else parcel
            for parcel in world.parcels
        ),
    )


def test_building_an_ad_uses_up_the_materials_and_starts_its_first_cycle() -> None:
    world = perform(replace(WORLD, time=12.0), BuildAd("a"))

    assert item_count(world, "a", MATERIAL) == 0
    assert world.parcels[0].facility == Facility("ad", 12.0, 12.0 + FACILITY_SECONDS)
    assert next_facility_cycle(world) == 12.0 + FACILITY_SECONDS


@pytest.mark.parametrize(
    ("world", "code"),
    [
        (replace(WORLD, parcels=(Parcel(5, 3, "b"),)), "not_your_parcel"),
        (replace(WORLD, parcels=()), "not_your_parcel"),
        (with_ads(WORLD, "a"), "parcel_built"),
        (
            replace(WORLD, residents=(resident("a", 55, 35, materials=7), WORLD.residents[1])),
            "not_enough_materials",
        ),
    ],
)
def test_an_ad_that_cannot_be_built_is_refused(world: World, code: str) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuildAd("a"))

    assert rejected.value.code == code


def test_an_ad_pays_its_owner_for_the_footfall_every_cycle() -> None:
    world = settle_due(replace(with_ads(WORLD, "a"), time=FACILITY_SECONDS))

    # 102.5 visitors at 30s, 84.2% of them this far out, plus a standing by: 2 x 96.3 coins.
    assert coin_balance(world, "a") == 100 + 192
    facility = world.parcels[0].facility
    assert facility is not None
    assert (facility.last_income, facility.earned, facility.next_at) == (192, 192, 60.0)
    last = world.ledger.transfers[-1]
    assert (last.payer, last.payee, last.amount, last.channel) == (SYSTEM, "a", 192, "visitors")


def test_ads_near_each_other_split_the_footfall() -> None:
    world = replace(with_ads(WORLD, "a", "b"), time=FACILITY_SECONDS)

    assert [ad_income(world, 5, 3), ad_income(world, 6, 3)] == [96, 90]


def test_residents_standing_by_add_to_the_footfall_but_walkers_do_not() -> None:
    walking = perform(replace(WORLD, time=FACILITY_SECONDS), MoveTo("a", Position(95, 95)))

    assert ad_income(replace(WORLD, time=FACILITY_SECONDS), 5, 3) == 192
    assert ad_income(walking, 5, 3) == 172


def test_visitors_thin_out_away_from_the_market_down_to_a_quarter() -> None:
    nobody = replace(WORLD, residents=(), parcels=())

    shares = [footfall(nobody, Position(50, 50 + distance)) for distance in (0, 37.5, 75, 90)]

    assert shares == [100, 62.5, 25, 25]


def test_an_ad_whose_owner_left_no_longer_shares_the_footfall() -> None:
    world = perform(replace(with_ads(WORLD, "a", "b"), time=FACILITY_SECONDS), LeaveGame("b"))

    assert ad_income(world, 5, 3) == 192


def test_visitors_grow_as_the_game_goes_on() -> None:
    assert [visitors(0), visitors(600), visitors(1_200)] == [100, 150, 200]


def test_an_owner_who_left_earns_nothing_more() -> None:
    left = perform(with_ads(WORLD, "a"), LeaveGame("a"))

    world = settle_due(replace(left, time=FACILITY_SECONDS))

    assert coin_balance(world, "a") == 100
    assert world.parcels[0].facility is not None
    assert world.parcels[0].facility.last_income == 0


def test_an_ad_pays_nothing_once_the_game_is_over() -> None:
    ended = end_game(with_ads(WORLD, "a"))

    world = settle_due(replace(ended, time=FACILITY_SECONDS))

    assert coin_balance(world, "a") == 100
