"""Energy: a resident's stamina, spent on walking and harvested from springs on the ground.

Energy is an item kind, `ENERGY`, so residents carry it in their inventory and trade it
like any other item through the offers in `trade.py`. Walking costs energy by distance, see
`walk_cost`, and speaking costs a few units per line, see `speech.py`; springs pay it back for
time spent harvesting, see `harvesting.py`. A resident whose energy runs out is out of the game, see
`lifecycle.knock_out_exhausted`.

Like inventories, these are server-side building blocks: every function returns a new world
and leaves its argument untouched; on error nothing changes.
"""

import math
from typing import Literal

from ..core.inventory import InventoryError, item_count, take_item
from ..core.world import Position, World
from .trade import reserved_items

ENERGY = "energy"
"""The item kind of energy."""

ENERGY_PER_UNIT = 0.5
"""Energy a walk costs per map unit of straight-line distance, before rounding up."""

EnergyErrorCode = Literal["not_enough_energy"]


class EnergyError(Exception):
    def __init__(self, code: EnergyErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: EnergyErrorCode = code
        self.message = message


def energy_total(world: World, resident_id: str) -> int:
    """Energy the resident carries plus those set aside in sale offers: what they own."""
    return item_count(world, resident_id, ENERGY) + reserved_items(world, resident_id, ENERGY)


def walk_cost(start: Position, end: Position) -> int:
    """Whole energy to walk from `start` to `end` in a straight line; standing still is free."""
    distance = math.dist((start.x, start.y), (end.x, end.y))
    # The slack keeps float noise, as in 3-4-5 = 5.000000000000001, from adding an energy.
    return max(math.ceil(distance * ENERGY_PER_UNIT - 1e-9), 0)


def pay_for_walk(world: World, resident_id: str, start: Position, end: Position) -> World:
    """Take the walk's cost out of the resident's carried energy; all or nothing."""
    return pay_energy(world, resident_id, walk_cost(start, end), "the walk")


def pay_energy(world: World, resident_id: str, cost: int, what: str) -> World:
    """Take `cost` energy out of the resident's carried energy, those set aside in offers not
    counting; all or nothing. `what` names what is paid for, in the refusal."""
    if cost == 0:
        return world
    try:
        return take_item(world, resident_id, ENERGY, cost)
    except InventoryError as error:
        if error.code != "not_enough_items":
            raise
        held = item_count(world, resident_id, ENERGY)
        raise EnergyError(
            "not_enough_energy",
            f"resident {resident_id!r} carries {held} energy, {what} costs {cost}",
        ) from error
