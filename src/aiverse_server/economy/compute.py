"""Compute: a resident's stamina, spent on walking and harvested from springs on the ground.

Compute is an item kind, `COMPUTE`, so residents carry it in their inventory and trade it
like any other item through the offers in `trade.py`. Walking costs compute by distance, see
`walk_cost`, and speaking costs a few units per line, see `speech.py`; springs pay it back for
time spent harvesting, see `harvesting.py`. A resident whose compute runs out is out of the
game, see `lifecycle.knock_out_exhausted`.

Like inventories, these are server-side building blocks: every function returns a new world
and leaves its argument untouched; on error nothing changes.
"""

import math
from typing import Literal

from ..core.inventory import InventoryError, item_count, take_item
from ..core.orders import order_items
from ..core.world import Position, World
from .trade import reserved_items

COMPUTE = "compute"
"""The item kind of compute."""

COMPUTE_PER_UNIT = 0.5
"""Compute a walk costs per map unit of straight-line distance, before rounding up."""

ComputeErrorCode = Literal["not_enough_compute"]


class ComputeError(Exception):
    def __init__(self, code: ComputeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ComputeErrorCode = code
        self.message = message


def compute_total(world: World, resident_id: str) -> int:
    """Compute the resident carries plus what sale offers and sell orders set aside: what they
    own."""
    return (
        item_count(world, resident_id, COMPUTE)
        + reserved_items(world, resident_id, COMPUTE)
        + order_items(world.orders, resident_id, COMPUTE)
    )


def walk_cost(start: Position, end: Position) -> int:
    """Whole compute to walk from `start` to `end` in a straight line; standing still is free."""
    distance = math.dist((start.x, start.y), (end.x, end.y))
    # The slack keeps float noise, as in 3-4-5 = 5.000000000000001, from adding a unit of compute.
    return max(math.ceil(distance * COMPUTE_PER_UNIT - 1e-9), 0)


def pay_for_walk(world: World, resident_id: str, start: Position, end: Position) -> World:
    """Take the walk's cost out of the resident's carried compute; all or nothing."""
    return pay_compute(world, resident_id, walk_cost(start, end), "the walk")


def pay_compute(world: World, resident_id: str, cost: int, what: str) -> World:
    """Take `cost` compute out of the resident's carried compute, those set aside in offers not
    counting; all or nothing. `what` names what is paid for, in the refusal."""
    if cost == 0:
        return world
    try:
        return take_item(world, resident_id, COMPUTE, cost)
    except InventoryError as error:
        if error.code != "not_enough_items":
            raise
        held = item_count(world, resident_id, COMPUTE)
        raise ComputeError(
            "not_enough_compute",
            f"resident {resident_id!r} carries {held} compute, {what} costs {cost}",
        ) from error
