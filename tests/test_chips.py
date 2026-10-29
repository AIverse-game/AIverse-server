from dataclasses import replace

import pytest

from aiverse_server.actions import (
    ActionRejected,
    BuyChip,
    LeaveGame,
    Mine,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    RespondToEvent,
    perform,
)
from aiverse_server.chips import CHIP_MODELS, ChipModel, best_chip
from aiverse_server.clock import settle_due
from aiverse_server.inventory import item_count
from aiverse_server.layout import create_default_world
from aiverse_server.lifecycle import end_game
from aiverse_server.mining import MINE_SECONDS
from aiverse_server.perception import begin_turn
from aiverse_server.tokens import TOKEN
from aiverse_server.world import Inventory, Market, Pool, Position, Resident, World, WorldMap

MK1, MK2, MK3 = CHIP_MODELS


def resident(resident_id: str, x: float, y: float, coins: int = 10_000) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((TOKEN, 100),)),
        coins=coins,
    )


# a and b stand at the market, c far from it next to a pool; nobody owns a chip yet.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 50, 50), resident("b", 54, 53, coins=299), resident("c", 10, 10)),
    pools=(Pool("p1", Position(10, 10), radius=4, reserve=500),),
    market=Market(Position(50, 50), radius=5),
    time=7.0,
)


def coins(world: World, resident_id: str) -> int:
    return next(r.coins for r in world.residents if r.id == resident_id)


def test_three_models_the_better_dearer_and_digging_more() -> None:
    assert [model.model for model in CHIP_MODELS] == ["MK1", "MK2", "MK3"]
    assert MK1.price < MK2.price < MK3.price
    assert MK1.tokens_per_job < MK2.tokens_per_job < MK3.tokens_per_job


@pytest.mark.parametrize("model", CHIP_MODELS, ids=lambda model: model.model)
def test_a_chip_costs_its_fixed_price_at_the_market(model: ChipModel) -> None:
    world = perform(WORLD, BuyChip("a", model.model))
    assert coins(world, "a") == 10_000 - model.price
    assert item_count(world, "a", model.model) == 1
    world = perform(world, BuyChip("a", model.model))
    assert coins(world, "a") == 10_000 - 2 * model.price
    assert item_count(world, "a", model.model) == 2


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 53, 54),))
    assert item_count(perform(world, BuyChip("a", "MK1")), "a", "MK1") == 1


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, BuyChip("c", "MK1"), "not_at_market"),
        (replace(WORLD, market=None), BuyChip("a", "MK1"), "not_at_market"),
        (WORLD, BuyChip("a", "MK4"), "unknown_chip"),
        (WORLD, BuyChip("a", "mk1"), "unknown_chip"),
        (WORLD, BuyChip("a", TOKEN), "unknown_chip"),
        (WORLD, BuyChip("b", "MK1"), "not_enough_coins"),
        (WORLD, BuyChip("nobody", "MK1"), "unknown_resident"),
        (perform(WORLD, LeaveGame("a")), BuyChip("a", "MK1"), "resident_left"),
        (end_game(WORLD), BuyChip("a", "MK1"), "game_over"),
        (perform(WORLD, MoveTo("a", Position(51, 50))), BuyChip("a", "MK1"), "busy"),
    ],
)
def test_purchases_that_cannot_go_through_are_refused(
    world: World, action: BuyChip, code: str
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(world, action)
    assert raised.value.code == code


def test_coins_set_aside_in_an_offer_cannot_pay_for_a_chip() -> None:
    world = replace(WORLD, residents=(resident("a", 50, 50, coins=MK1.price), WORLD.residents[2]))
    world = perform(world, OfferToBuy("a", "c", TOKEN, 1, 1))
    with pytest.raises(ActionRejected) as raised:
        perform(world, BuyChip("a", "MK1"))
    assert raised.value.code == "not_enough_coins"


def test_a_bought_chip_mines_and_the_best_one_carried_does_the_work() -> None:
    world = perform(perform(WORLD, BuyChip("a", "MK1")), BuyChip("a", "MK2"))
    assert best_chip(world, "a") == MK2
    assert best_chip(world, "c") is None
    world = perform(world, MoveTo("a", Position(10, 10)))
    world = settle_due(replace(world, time=100.0))
    world = perform(world, Mine("a", "p1"))
    job = world.residents[0].mining
    assert job is not None
    assert (job.chip, job.amount) == ("MK2", MK2.tokens_per_job)
    world = settle_due(replace(world, time=100.0 + MINE_SECONDS))
    assert item_count(world, "a", TOKEN) == 100 - 57 + MK2.tokens_per_job
    assert item_count(world, "a", "MK1") == item_count(world, "a", "MK2") == 1


def test_chips_change_hands_through_offers() -> None:
    world = perform(WORLD, BuyChip("a", "MK3"))
    world = perform(world, OfferToSell("a", "c", "MK3", 1, 2_500))
    world = perform(world, RespondToEvent("c", 1, accept=True))
    assert item_count(world, "a", "MK3") == 0
    assert coins(world, "a") == 10_000 - MK3.price + 2_500
    job = perform(world, Mine("c", "p1")).residents[2].mining
    assert job is not None
    assert job.chip == "MK3"


def test_leaving_loses_the_chips() -> None:
    world = perform(perform(WORLD, BuyChip("a", "MK2")), LeaveGame("a"))
    assert item_count(world, "a", "MK2") == 0
    assert coins(world, "a") == 10_000 - MK2.price


def test_perception_shows_the_chips_on_sale_from_anywhere() -> None:
    _, seen = begin_turn(WORLD, "c")
    assert seen.market is not None
    assert seen.market.chips == CHIP_MODELS


def test_nobody_starts_with_a_chip() -> None:
    world = create_default_world(1)
    for someone in world.residents:
        assert best_chip(world, someone.id) is None
    pool = world.pools[0]
    first = replace(world.residents[0], position=pool.position)
    world = replace(world, residents=(first, *world.residents[1:]))
    with pytest.raises(ActionRejected) as raised:
        perform(world, Mine(first.id, pool.id))
    assert raised.value.code == "no_chip"
