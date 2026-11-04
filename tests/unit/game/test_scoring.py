"""The score the ranking counts: coins plus what energy and crystals are worth."""

from dataclasses import replace

import pytest

from aiverse_server.core.towers import Tower, TowerSlot
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    Resident,
    Spring,
    World,
    WorldMap,
)
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.market import MARKET_BASE_PRICE, unit_price
from aiverse_server.economy.shards import SHARD
from aiverse_server.game.actions import OfferToSell, PlaceOrder, perform
from aiverse_server.game.lifecycle import end_game, item_value, leave_game, ranks, score

HERE = Position(10, 10)
# An untouched market pays its base price, 20 coins an energy.
WORLD = World(
    map=WorldMap(size=50),
    residents=(
        Resident("a", "甲", "#112233", HERE, Inventory(((ENERGY, 100), ("C1", 1))), coins=1000),
        Resident("b", "乙", "#445566", HERE, Inventory(((ENERGY, 10),)), coins=3000),
    ),
    springs=(Spring("s1", HERE, 4, 1000),),
    market=Market(HERE, 5),
)


def test_energy_counts_at_the_market_price_and_crystals_at_half_their_price() -> None:
    assert item_value(WORLD, "a") == 100 * MARKET_BASE_PRICE + 600 // 2


def test_energy_counts_at_the_price_the_market_has_climbed_to() -> None:
    quiet = replace(WORLD, time=600.0)
    price = unit_price(quiet.market, 600.0)  # type: ignore[arg-type]

    value = item_value(quiet, "a")

    assert price > MARKET_BASE_PRICE
    assert value == int(100 * price + 600 // 2) == 4296


def test_energy_counts_for_nothing_without_a_market() -> None:
    world = replace(WORLD, market=None)

    assert item_value(world, "a") == 600 // 2


def test_shards_count_for_nothing() -> None:
    a = replace(WORLD.residents[0], inventory=Inventory(((SHARD, 50),)))
    world = replace(WORLD, residents=(a, WORLD.residents[1]))

    assert item_value(world, "a") == 0


@pytest.mark.parametrize(
    "action",
    [OfferToSell("a", "b", "C1", 1, 100), PlaceOrder("a", "sell", "C1", 1, 5000)],
    ids=["offer", "sell_order"],
)
def test_a_crystal_set_aside_still_counts(action: object) -> None:
    world = perform(WORLD, action)  # type: ignore[arg-type]

    assert item_value(world, "a") == item_value(WORLD, "a")


def test_a_crystal_installed_in_a_tower_still_counts() -> None:
    a = replace(WORLD.residents[0], inventory=Inventory(((ENERGY, 100),)))
    tower = Tower(1, "b", "s1", (TowerSlot(1, "a", "C1", 0, 20.0),), next_slot_id=2)
    world = replace(WORLD, residents=(a, WORLD.residents[1]), towers=(tower,), next_tower_id=2)

    assert item_value(world, "a") == item_value(WORLD, "a")


def test_a_resident_who_left_has_nothing_left_to_count() -> None:
    world = leave_game(WORLD, "a")

    assert (item_value(world, "a"), score(world, "a")) == (0, 1000)


def test_the_ranking_counts_items_as_well_as_coins() -> None:
    # a has 1000 coins and 2300 in items, b 3000 coins and 200 in items.
    assert ranks(WORLD) == {"a": 1, "b": 2}


def test_items_handed_back_at_the_end_still_count() -> None:
    world = perform(WORLD, PlaceOrder("a", "sell", "C1", 1, 5000))

    ended = end_game(world)

    assert score(ended, "a") == score(WORLD, "a") == 1000 + 2300
