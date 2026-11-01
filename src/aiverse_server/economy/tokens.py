"""Tokens: a resident's stamina, spent on walking and mined from pools on the ground.

Tokens are an item kind, `TOKEN`, so residents carry them in their inventory and trade them
like any other item through the offers in `trade.py`. Walking costs tokens by distance, see
`walk_cost`, and speaking costs a few per line, see `speech.py`; pools pay them back for
time spent mining, see `mining.py`. A resident whose tokens run out is out of the game, see
`lifecycle.knock_out_exhausted`.

Like inventories, these are server-side building blocks: every function returns a new world
and leaves its argument untouched; on error nothing changes.
"""

import math
from typing import Literal

from ..core.inventory import InventoryError, item_count, take_item
from ..core.world import Position, World
from .trade import reserved_items

TOKEN = "token"
"""The item kind of tokens."""

TOKENS_PER_UNIT = 0.5
"""Tokens a walk costs per map unit of straight-line distance, before rounding up."""

TokenErrorCode = Literal["not_enough_tokens"]


class TokenError(Exception):
    def __init__(self, code: TokenErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: TokenErrorCode = code
        self.message = message


def token_total(world: World, resident_id: str) -> int:
    """Tokens the resident carries plus those set aside in sale offers: what they own."""
    return item_count(world, resident_id, TOKEN) + reserved_items(world, resident_id, TOKEN)


def walk_cost(start: Position, end: Position) -> int:
    """Whole tokens to walk from `start` to `end` in a straight line; standing still is free."""
    distance = math.dist((start.x, start.y), (end.x, end.y))
    # The slack keeps float noise, as in 3-4-5 = 5.000000000000001, from adding a token.
    return max(math.ceil(distance * TOKENS_PER_UNIT - 1e-9), 0)


def pay_for_walk(world: World, resident_id: str, start: Position, end: Position) -> World:
    """Take the walk's cost out of the resident's carried tokens; all or nothing."""
    return pay_tokens(world, resident_id, walk_cost(start, end), "the walk")


def pay_tokens(world: World, resident_id: str, cost: int, what: str) -> World:
    """Take `cost` tokens out of the resident's carried tokens, those set aside in offers not
    counting; all or nothing. `what` names what is paid for, in the refusal."""
    if cost == 0:
        return world
    try:
        return take_item(world, resident_id, TOKEN, cost)
    except InventoryError as error:
        if error.code != "not_enough_items":
            raise
        held = item_count(world, resident_id, TOKEN)
        raise TokenError(
            "not_enough_tokens",
            f"resident {resident_id!r} carries {held} tokens, {what} costs {cost}",
        ) from error
