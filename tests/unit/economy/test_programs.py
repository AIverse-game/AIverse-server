from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import Inventory, Market, Position, Resident, Spring, World, WorldMap
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.harvesting import HARVEST_SECONDS
from aiverse_server.economy.programs import PROGRAM_TIERS, ProgramTier, best_program
from aiverse_server.game.actions import (
    ActionRejected,
    BuyProgram,
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

v1, v2, v3 = PROGRAM_TIERS


def resident(resident_id: str, x: float, y: float, coins: int = 20_000) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((COMPUTE, 100),)),
        coins=coins,
    )


# a and b stand at the market, c far from it next to a spring; nobody owns a program yet.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 50, 50), resident("b", 54, 53, coins=299), resident("c", 10, 10)),
    springs=(Spring("p1", Position(10, 10), radius=4, reserve=500),),
    market=Market(Position(50, 50), radius=5),
    time=7.0,
)


def coins(world: World, resident_id: str) -> int:
    return next(r.coins for r in world.residents if r.id == resident_id)


def test_three_tiers_the_better_dearer_and_drawing_more() -> None:
    assert [tier.tier for tier in PROGRAM_TIERS] == ["v1", "v2", "v3"]
    assert v1.price < v2.price < v3.price
    assert v1.compute_per_job < v2.compute_per_job < v3.compute_per_job


@pytest.mark.parametrize("tier", PROGRAM_TIERS, ids=lambda tier: tier.tier)
def test_a_program_costs_its_fixed_price_at_the_market(tier: ProgramTier) -> None:
    once = perform(WORLD, BuyProgram("a", tier.tier))
    twice = perform(once, BuyProgram("a", tier.tier))

    assert coins(once, "a") == 20_000 - tier.price
    assert item_count(once, "a", tier.tier) == 1
    assert coins(twice, "a") == 20_000 - 2 * tier.price
    assert item_count(twice, "a", tier.tier) == 2


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 53, 54),))
    assert item_count(perform(world, BuyProgram("a", "v1")), "a", "v1") == 1


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, BuyProgram("c", "v1"), "not_at_market"),
        (replace(WORLD, market=None), BuyProgram("a", "v1"), "not_at_market"),
        (WORLD, BuyProgram("a", "v4"), "unknown_program"),
        (WORLD, BuyProgram("a", "V1"), "unknown_program"),
        (WORLD, BuyProgram("a", COMPUTE), "unknown_program"),
        (WORLD, BuyProgram("b", "v1"), "not_enough_coins"),
        (WORLD, BuyProgram("nobody", "v1"), "unknown_resident"),
        (perform(WORLD, LeaveGame("a")), BuyProgram("a", "v1"), "resident_left"),
        (end_game(WORLD), BuyProgram("a", "v1"), "game_over"),
        (perform(WORLD, MoveTo("a", Position(51, 50))), BuyProgram("a", "v1"), "busy"),
    ],
)
def test_purchases_that_cannot_go_through_are_refused(
    world: World, action: BuyProgram, code: str
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(world, action)
    assert raised.value.code == code


def test_coins_set_aside_in_an_offer_cannot_pay_for_a_program() -> None:
    world = replace(WORLD, residents=(resident("a", 50, 50, coins=v1.price), WORLD.residents[2]))
    world = perform(world, OfferToBuy("a", "c", COMPUTE, 1, 1))
    with pytest.raises(ActionRejected) as raised:
        perform(world, BuyProgram("a", "v1"))
    assert raised.value.code == "not_enough_coins"


def test_the_best_program_carried_is_the_one_that_gathers_most() -> None:
    world = perform(perform(WORLD, BuyProgram("a", "v1")), BuyProgram("a", "v2"))

    assert best_program(world, "a") == v2
    assert best_program(world, "c") is None


def test_a_bought_program_harvests_and_the_best_one_carried_does_the_work() -> None:
    bought = perform(perform(WORLD, BuyProgram("a", "v1")), BuyProgram("a", "v2"))

    walked = settle_due(replace(perform(bought, MoveTo("a", Position(10, 10))), time=100.0))
    harvesting = perform(walked, Harvest("a", "p1"))
    paid = settle_due(replace(harvesting, time=100.0 + HARVEST_SECONDS))

    job = harvesting.residents[0].harvesting
    assert job is not None
    assert (job.program, job.amount) == ("v2", v2.compute_per_job)
    # The walk from the market to the spring cost 29 compute.
    assert item_count(paid, "a", COMPUTE) == 100 - 29 + v2.compute_per_job
    assert item_count(paid, "a", "v1") == item_count(paid, "a", "v2") == 1


def test_programs_change_hands_through_offers() -> None:
    world = perform(WORLD, BuyProgram("a", "v3"))
    world = perform(world, OfferToSell("a", "c", "v3", 1, 2_500))

    traded = perform(world, RespondToEvent("c", 1, accept=True))
    job = perform(traded, Harvest("c", "p1")).residents[2].harvesting

    assert item_count(traded, "a", "v3") == 0
    assert coins(traded, "a") == 20_000 - v3.price + 2_500
    assert job is not None
    assert job.program == "v3"


def test_leaving_loses_the_programs() -> None:
    world = perform(perform(WORLD, BuyProgram("a", "v2")), LeaveGame("a"))
    assert item_count(world, "a", "v2") == 0
    assert coins(world, "a") == 20_000 - v2.price


def test_perception_shows_the_programs_on_sale_from_anywhere() -> None:
    _, seen = begin_turn(WORLD, "c")
    assert seen.market is not None
    assert seen.market.programs == PROGRAM_TIERS


def test_nobody_starts_with_a_program() -> None:
    world = create_default_world(1)

    assert all(best_program(world, someone.id) is None for someone in world.residents)


def test_nobody_can_harvest_at_the_start() -> None:
    world = create_default_world(1)
    spring = world.springs[0]
    first = replace(world.residents[0], position=spring.position)
    at_a_spring = replace(world, residents=(first, *world.residents[1:]))

    with pytest.raises(ActionRejected) as raised:
        perform(at_a_spring, Harvest(first.id, spring.id))

    assert raised.value.code == "no_program"
