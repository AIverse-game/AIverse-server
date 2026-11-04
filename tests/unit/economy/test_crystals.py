from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import Inventory, Market, Position, Resident, Spring, World, WorldMap
from aiverse_server.economy.crystals import CRYSTAL_TIERS, CrystalTier, best_crystal
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.harvesting import HARVEST_SECONDS
from aiverse_server.game.actions import (
    ActionRejected,
    BuyCrystal,
    Harvest,
    LeaveGame,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    RespondToEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.layout import create_default_world
from aiverse_server.game.lifecycle import end_game
from aiverse_server.residents.perception import begin_turn

C1, C2, C3 = CRYSTAL_TIERS


def resident(resident_id: str, x: float, y: float, coins: int = 10_000) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((ENERGY, 100),)),
        coins=coins,
    )


# a and b stand at the market, c far from it next to a spring; nobody owns a crystal yet.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 50, 50), resident("b", 54, 53, coins=299), resident("c", 10, 10)),
    springs=(Spring("p1", Position(10, 10), radius=4, reserve=500),),
    market=Market(Position(50, 50), radius=5),
    time=7.0,
)


def coins(world: World, resident_id: str) -> int:
    return next(r.coins for r in world.residents if r.id == resident_id)


def test_three_models_the_better_dearer_and_digging_more() -> None:
    assert [tier.tier for tier in CRYSTAL_TIERS] == ["C1", "C2", "C3"]
    assert C1.price < C2.price < C3.price
    assert C1.energy_per_job < C2.energy_per_job < C3.energy_per_job


@pytest.mark.parametrize("tier", CRYSTAL_TIERS, ids=lambda tier: tier.tier)
def test_a_crystal_costs_its_fixed_price_at_the_market(tier: CrystalTier) -> None:
    once = perform(WORLD, BuyCrystal("a", tier.tier))
    twice = perform(once, BuyCrystal("a", tier.tier))

    assert coins(once, "a") == 10_000 - tier.price
    assert item_count(once, "a", tier.tier) == 1
    assert coins(twice, "a") == 10_000 - 2 * tier.price
    assert item_count(twice, "a", tier.tier) == 2


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 53, 54),))
    assert item_count(perform(world, BuyCrystal("a", "C1")), "a", "C1") == 1


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, BuyCrystal("c", "C1"), "not_at_market"),
        (replace(WORLD, market=None), BuyCrystal("a", "C1"), "not_at_market"),
        (WORLD, BuyCrystal("a", "C4"), "unknown_crystal"),
        (WORLD, BuyCrystal("a", "c1"), "unknown_crystal"),
        (WORLD, BuyCrystal("a", ENERGY), "unknown_crystal"),
        (WORLD, BuyCrystal("b", "C1"), "not_enough_coins"),
        (WORLD, BuyCrystal("nobody", "C1"), "unknown_resident"),
        (perform(WORLD, LeaveGame("a")), BuyCrystal("a", "C1"), "resident_left"),
        (end_game(WORLD), BuyCrystal("a", "C1"), "game_over"),
        (perform(WORLD, MoveTo("a", Position(51, 50))), BuyCrystal("a", "C1"), "busy"),
    ],
)
def test_purchases_that_cannot_go_through_are_refused(
    world: World, action: BuyCrystal, code: str
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(world, action)
    assert raised.value.code == code


def test_coins_set_aside_in_an_offer_cannot_pay_for_a_crystal() -> None:
    world = replace(WORLD, residents=(resident("a", 50, 50, coins=C1.price), WORLD.residents[2]))
    world = perform(world, OfferToBuy("a", "c", ENERGY, 1, 1))
    with pytest.raises(ActionRejected) as raised:
        perform(world, BuyCrystal("a", "C1"))
    assert raised.value.code == "not_enough_coins"


def test_the_best_crystal_carried_is_the_one_that_digs_most() -> None:
    world = perform(perform(WORLD, BuyCrystal("a", "C1")), BuyCrystal("a", "C2"))

    assert best_crystal(world, "a") == C2
    assert best_crystal(world, "c") is None


def test_a_bought_crystal_harvests_and_the_best_one_carried_does_the_work() -> None:
    bought = perform(perform(WORLD, BuyCrystal("a", "C1")), BuyCrystal("a", "C2"))

    walked = settle_due(replace(perform(bought, MoveTo("a", Position(10, 10))), time=100.0))
    harvesting = perform(walked, Harvest("a", "p1"))
    paid = settle_due(replace(harvesting, time=100.0 + HARVEST_SECONDS))

    job = harvesting.residents[0].harvesting
    assert job is not None
    assert (job.crystal, job.amount) == ("C2", C2.energy_per_job)
    # The walk from the market to the spring cost 29 energy.
    assert item_count(paid, "a", ENERGY) == 100 - 29 + C2.energy_per_job
    assert item_count(paid, "a", "C1") == item_count(paid, "a", "C2") == 1


def test_crystals_change_hands_through_offers() -> None:
    world = perform(WORLD, BuyCrystal("a", "C3"))
    world = perform(world, OfferToSell("a", "c", "C3", 1, 2_500))

    traded = perform(world, RespondToEvent("c", 1, accept=True))
    job = perform(traded, Harvest("c", "p1")).residents[2].harvesting

    assert item_count(traded, "a", "C3") == 0
    assert coins(traded, "a") == 10_000 - C3.price + 2_500
    assert job is not None
    assert job.crystal == "C3"


def test_leaving_loses_the_crystals() -> None:
    world = perform(perform(WORLD, BuyCrystal("a", "C2")), LeaveGame("a"))
    assert item_count(world, "a", "C2") == 0
    assert coins(world, "a") == 10_000 - C2.price


def test_perception_shows_the_crystals_on_sale_from_anywhere() -> None:
    _, seen = begin_turn(WORLD, "c")
    assert seen.market is not None
    assert seen.market.crystals == CRYSTAL_TIERS


def test_nobody_starts_with_a_crystal() -> None:
    world = create_default_world(1)

    assert all(best_crystal(world, someone.id) is None for someone in world.residents)


def test_nobody_can_harvest_at_the_start() -> None:
    world = create_default_world(1)
    spring = world.springs[0]
    first = replace(world.residents[0], position=spring.position)
    at_a_spring = replace(world, residents=(first, *world.residents[1:]))

    with pytest.raises(ActionRejected) as raised:
        perform(at_a_spring, Harvest(first.id, spring.id))

    assert raised.value.code == "no_crystal"
