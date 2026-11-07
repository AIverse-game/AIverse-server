"""Resident inventories: store, take, count, list and clear items, and pay or earn coins.

These are server-side building blocks, not operation primitives: clients and AI cannot call
them directly. Game rules such as trading will be primitives in
`actions.py` that use them and turn `InventoryError` into a rejection.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes. Inventories live only as long as the in-memory world,
so a new game starts empty, and `clear_items` empties a resident who leaves the game.

Coins are a balance of their own rather than an item kind: they decide the ranking, and a
resident who leaves keeps them.
"""

from dataclasses import replace
from typing import Literal

from .world import Inventory, Resident, World

InventoryErrorCode = Literal["unknown_resident", "not_enough_items", "not_enough_coins"]


class InventoryError(Exception):
    def __init__(self, code: InventoryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: InventoryErrorCode = code
        self.message = message


def list_items(world: World, resident_id: str) -> dict[str, int]:
    """Item kind to count, sorted by kind; empty when the resident carries nothing."""
    return _find(world, resident_id).inventory.counts()


def item_count(world: World, resident_id: str, kind: str) -> int:
    _check_kind(kind)
    return _find(world, resident_id).inventory.count(kind)


def store_item(world: World, resident_id: str, kind: str, quantity: int = 1) -> World:
    """Add `quantity` items of `kind` to the resident's inventory."""
    _check_kind(kind)
    _check_quantity(quantity)
    counts = _find(world, resident_id).inventory.counts()
    counts[kind] = counts.get(kind, 0) + quantity
    return _with_inventory(world, resident_id, counts)


def take_item(world: World, resident_id: str, kind: str, quantity: int = 1) -> World:
    """Remove `quantity` items of `kind`; all or nothing, so a shortfall removes none."""
    _check_kind(kind)
    _check_quantity(quantity)
    counts = _find(world, resident_id).inventory.counts()
    held = counts.get(kind, 0)
    if held < quantity:
        raise InventoryError(
            "not_enough_items",
            f"resident {resident_id!r} holds {held} of {kind!r}, cannot take {quantity}",
        )
    if held == quantity:
        del counts[kind]
    else:
        counts[kind] = held - quantity
    return _with_inventory(world, resident_id, counts)


def clear_items(world: World, resident_id: str) -> World:
    """Empty the resident's inventory, as when they leave the game."""
    _find(world, resident_id)
    return _with_inventory(world, resident_id, {})


def coin_balance(world: World, resident_id: str) -> int:
    """Spendable coins, not counting those set aside by pending offers."""
    return _find(world, resident_id).coins


def add_coins(world: World, resident_id: str, amount: int) -> World:
    _check_quantity(amount)
    resident = _find(world, resident_id)
    return _with_coins(world, resident_id, resident.coins + amount)


def take_coins(world: World, resident_id: str, amount: int) -> World:
    """Remove `amount` coins; all or nothing, so a shortfall removes none."""
    _check_quantity(amount)
    held = _find(world, resident_id).coins
    if held < amount:
        raise InventoryError(
            "not_enough_coins", f"resident {resident_id!r} has {held} coins, cannot pay {amount}"
        )
    return _with_coins(world, resident_id, held - amount)


def _check_quantity(quantity: int) -> None:
    # Exactly int: True is a mistake rather than one item, and NaN would pass `< 1`.
    if type(quantity) is not int or quantity < 1:
        raise ValueError(f"quantity must be a positive integer, got {quantity!r}")


def _check_kind(kind: str) -> None:
    if not kind:
        raise ValueError("item kind must not be empty")


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise InventoryError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_coins(world: World, resident_id: str, coins: int) -> World:
    residents = tuple(
        replace(resident, coins=coins) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def _with_inventory(world: World, resident_id: str, counts: dict[str, int]) -> World:
    inventory = Inventory(tuple(counts.items()))
    residents = tuple(
        replace(resident, inventory=inventory) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
