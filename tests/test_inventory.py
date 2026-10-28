import math
from collections.abc import Callable
from typing import Any

import pytest

from aiverse_server.inventory import (
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
from aiverse_server.layout import STARTING_TOKENS, create_default_world
from aiverse_server.tokens import TOKEN
from aiverse_server.world import Inventory, Position, Resident, World, WorldMap

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


def test_residents_start_with_nothing_but_their_tokens() -> None:
    assert list_items(WORLD, "a") == {}
    world = create_default_world()
    assert all(list_items(world, r.id) == {TOKEN: STARTING_TOKENS} for r in world.residents)


def test_list_items_is_sorted_by_kind_and_a_copy() -> None:
    items = list_items(WORLD, "b")
    assert list(items.items()) == [("bread", 1), ("wood", 2)]
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
    after = take_item(WORLD, "b", "wood")
    assert list_items(after, "b") == {"bread": 1, "wood": 1}
    after = take_item(after, "b", "wood", 1)
    assert list_items(after, "b") == {"bread": 1}
    assert after.residents[1].inventory == Inventory((("bread", 1),))


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


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, math.nan])
def test_quantities_must_be_positive_integers(quantity: int) -> None:
    with pytest.raises(ValueError, match="quantity"):
        store_item(WORLD, "a", "wood", quantity)
    with pytest.raises(ValueError, match="quantity"):
        take_item(WORLD, "b", "wood", quantity)


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


def test_coins_are_paid_and_earned_all_or_nothing() -> None:
    world = add_coins(WORLD, "a", 30)
    assert coin_balance(world, "a") == 30
    world = take_coins(world, "a", 30)
    assert coin_balance(world, "a") == 0
    with pytest.raises(InventoryError) as raised:
        take_coins(world, "a", 1)
    assert raised.value.code == "not_enough_coins"
    assert coin_balance(WORLD, "a") == 0


def test_coins_are_not_items_and_survive_clearing() -> None:
    world = clear_items(add_coins(WORLD, "a", 5), "a")
    assert coin_balance(world, "a") == 5
    assert list_items(world, "a") == {}


@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_coin_amounts_must_be_positive_integers(amount: Any) -> None:
    for change in (add_coins, take_coins):
        with pytest.raises(ValueError):
            change(WORLD, "a", amount)


def test_coin_functions_refuse_unknown_residents() -> None:
    for call in (lambda: coin_balance(WORLD, "x"), lambda: add_coins(WORLD, "x", 1)):
        with pytest.raises(InventoryError) as raised:
            call()
        assert raised.value.code == "unknown_resident"
