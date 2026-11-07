import math
from collections.abc import Callable
from typing import Any

import pytest

from aiverse_server.core.inventory import (
    InventoryError,
    add_coins,
    clear_items,
    coin_balance,
    item_count,
    list_items,
    store_item,
    take_coins,
    take_item,
)
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.game.layout import STARTING_COMPUTE, create_default_world

WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(id="a", name="a", color="#ffffff", position=Position(1, 1)),
        Resident(
            id="b",
            name="b",
            color="#000000",
            position=Position(9, 9),
            inventory=Inventory((("wood", 2), ("bread", 1))),
        ),
    ),
)


def test_residents_start_with_nothing_but_their_compute() -> None:
    assert list_items(WORLD, "a") == {}
    world = create_default_world()
    assert all(list_items(world, r.id) == {COMPUTE: STARTING_COMPUTE} for r in world.residents)


def test_list_items_is_sorted_by_kind() -> None:
    assert list(list_items(WORLD, "b").items()) == [("bread", 1), ("wood", 2)]


def test_changing_the_listed_items_leaves_the_world_alone() -> None:
    items = list_items(WORLD, "b")

    items["bread"] = 99

    assert item_count(WORLD, "b", "bread") == 1


def test_item_count_is_zero_for_items_not_held() -> None:
    assert item_count(WORLD, "a", "wood") == 0
    assert item_count(WORLD, "b", "wood") == 2


def test_store_item_adds_to_only_that_resident() -> None:
    after = store_item(WORLD, "a", "grain", 3)
    after = store_item(after, "a", "grain")
    assert list_items(after, "a") == {"grain": 4}
    assert after.residents[1] == WORLD.residents[1]
    assert list_items(WORLD, "a") == {}


def test_store_item_has_no_capacity_limit() -> None:
    after = store_item(WORLD, "a", "grain", 10**9)
    after = store_item(after, "a", "wood", 10**9)
    assert list_items(after, "a") == {"grain": 10**9, "wood": 10**9}


def test_take_item_removes_and_drops_empty_stacks() -> None:
    once = take_item(WORLD, "b", "wood")
    twice = take_item(once, "b", "wood", 1)

    assert list_items(once, "b") == {"bread": 1, "wood": 1}
    assert list_items(twice, "b") == {"bread": 1}
    assert twice.residents[1].inventory == Inventory((("bread", 1),))


@pytest.mark.parametrize(("kind", "quantity"), [("wood", 3), ("grain", 1)])
def test_take_item_rejects_a_shortfall_and_takes_nothing(kind: str, quantity: int) -> None:
    with pytest.raises(InventoryError) as raised:
        take_item(WORLD, "b", kind, quantity)
    assert raised.value.code == "not_enough_items"
    assert list_items(WORLD, "b") == {"bread": 1, "wood": 2}


def test_clear_items_empties_one_resident() -> None:
    after = clear_items(store_item(WORLD, "a", "grain", 2), "b")
    assert list_items(after, "b") == {}
    assert list_items(after, "a") == {"grain": 2}


@pytest.mark.parametrize(
    "call",
    [
        lambda: list_items(WORLD, "nobody"),
        lambda: item_count(WORLD, "nobody", "wood"),
        lambda: store_item(WORLD, "nobody", "wood"),
        lambda: take_item(WORLD, "nobody", "wood"),
        lambda: clear_items(WORLD, "nobody"),
    ],
)
def test_unknown_residents_are_rejected(call: Callable[[], object]) -> None:
    with pytest.raises(InventoryError) as raised:
        call()
    assert raised.value.code == "unknown_resident"


@pytest.mark.parametrize("change", [store_item, take_item])
@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, math.nan])
def test_quantities_must_be_positive_integers(
    change: Callable[[World, str, str, int], World], quantity: int
) -> None:
    with pytest.raises(ValueError, match="quantity"):
        change(WORLD, "b", "wood", quantity)


@pytest.mark.parametrize(
    "call",
    [
        lambda: item_count(WORLD, "b", ""),
        lambda: store_item(WORLD, "a", ""),
        lambda: take_item(WORLD, "b", ""),
    ],
)
def test_item_kinds_must_not_be_empty(call: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match="empty"):
        call()


def test_inventory_equality_ignores_order() -> None:
    assert Inventory((("wood", 2), ("bread", 1))) == Inventory((("bread", 1), ("wood", 2)))


@pytest.mark.parametrize(
    "stacks",
    [
        (("wood", 1), ("wood", 2)),
        (("wood", 0),),
        (("wood", -1),),
        (("wood", True),),
        (("wood", math.nan),),
        (("", 1),),
    ],
)
def test_inventory_rejects_invalid_stacks(stacks: tuple[tuple[str, Any], ...]) -> None:
    with pytest.raises(ValueError):
        Inventory(stacks)


def test_coins_earned_and_paid_change_the_balance() -> None:
    earned = add_coins(WORLD, "a", 30)
    paid = take_coins(earned, "a", 30)

    assert coin_balance(earned, "a") == 30
    assert coin_balance(paid, "a") == 0
    assert coin_balance(WORLD, "a") == 0


def test_paying_more_coins_than_held_is_refused() -> None:
    world = add_coins(WORLD, "a", 30)

    with pytest.raises(InventoryError) as raised:
        take_coins(world, "a", 31)

    assert raised.value.code == "not_enough_coins"


def test_coins_are_not_items_and_survive_clearing() -> None:
    world = clear_items(add_coins(WORLD, "a", 5), "a")
    assert coin_balance(world, "a") == 5
    assert list_items(world, "a") == {}


@pytest.mark.parametrize("change", [add_coins, take_coins])
@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_coin_amounts_must_be_positive_integers(
    change: Callable[[World, str, int], World], amount: Any
) -> None:
    with pytest.raises(ValueError, match="quantity must be a positive integer"):
        change(WORLD, "a", amount)


@pytest.mark.parametrize(
    "call",
    [lambda: coin_balance(WORLD, "x"), lambda: add_coins(WORLD, "x", 1)],
    ids=["balance", "earning"],
)
def test_coin_functions_refuse_unknown_residents(call: Callable[[], object]) -> None:
    with pytest.raises(InventoryError) as raised:
        call()

    assert raised.value.code == "unknown_resident"
