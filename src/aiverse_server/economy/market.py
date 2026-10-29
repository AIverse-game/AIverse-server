"""The market: a buyer from outside the town who pays coins for tokens.

Apart from the coins residents start with, this is where new coins come from: trades
between residents only move coins around. A resident standing within the market's radius
sells tokens they carry with `sell_tokens` and is paid at the market price.

The price falls as tokens are sold and recovers as time passes. The market keeps a
pressure, the tokens sold recently, which halves every `MARKET_HALF_LIFE` seconds of game
time. The price of one more token at pressure S is `MARKET_BASE_PRICE * MARKET_DEPTH /
(MARKET_DEPTH + S)`, so a pressure of `MARKET_DEPTH` halves it. Selling q tokens pays that
price summed over the sale, from S to S + q, rounded down to whole coins; splitting a sale
into smaller ones therefore earns no more than selling at once, while waiting for the price
to recover does.

Pressure at any time follows from the last sale alone, so nothing has to happen on the clock:
`Market` records the pressure and the game time it was measured at.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from dataclasses import replace
from typing import Literal

from ..core.inventory import InventoryError, add_coins, take_item
from ..core.world import Market, Resident, World
from .tokens import TOKEN, TokenError

MARKET_BASE_PRICE = 20.0
"""Coins the market pays for a token when nobody has sold for a long while."""

MARKET_DEPTH = 100.0
"""Recent sales, in tokens, that halve the price."""

MARKET_HALF_LIFE = 60.0
"""Game seconds for the pressure of past sales, and so the price drop, to halve."""

MARKET_RADIUS = 5.0

MarketErrorCode = Literal["unknown_resident", "not_at_market", "invalid_quantity", "price_too_low"]


class MarketError(Exception):
    def __init__(self, code: MarketErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: MarketErrorCode = code
        self.message = message


def pressure_at(market: Market, time: float) -> float:
    """Recent sales in tokens at game time `time`, faded since the last sale."""
    elapsed = max(time - market.pressure_at, 0.0)
    return market.pressure * math.exp2(-elapsed / MARKET_HALF_LIFE)


def unit_price(market: Market, time: float) -> float:
    """Coins one more token would fetch at game time `time`, before rounding."""
    return MARKET_BASE_PRICE * MARKET_DEPTH / (MARKET_DEPTH + pressure_at(market, time))


def quote(market: Market, time: float, quantity: int) -> int:
    """Whole coins selling `quantity` tokens at game time `time` pays."""
    depth = MARKET_DEPTH + pressure_at(market, time)
    earned = MARKET_BASE_PRICE * MARKET_DEPTH * math.log1p(quantity / depth)
    # The slack keeps float noise just under a whole number from costing a coin.
    return math.floor(earned + 1e-9)


def sell_tokens(world: World, resident_id: str, quantity: int) -> World:
    """Sell `quantity` tokens the resident carries to the market they stand at, for coins."""
    resident = _find(world, resident_id)
    market = world.market
    if market is None or not market.reaches(resident.position):
        raise MarketError("not_at_market", f"resident {resident_id!r} is not at the market")
    # Exactly int: True is a mistake rather than one token.
    if type(quantity) is not int or quantity < 1:
        raise MarketError(
            "invalid_quantity", f"quantity must be a positive integer, got {quantity!r}"
        )
    try:
        world = take_item(world, resident_id, TOKEN, quantity)
    except InventoryError as error:
        if error.code != "not_enough_items":
            raise
        held = resident.inventory.count(TOKEN)
        raise TokenError(
            "not_enough_tokens", f"resident {resident_id!r} carries {held} tokens, not {quantity}"
        ) from error
    # Priced only once the tokens are known to be there: an untrusted quantity can be too big
    # to turn into a float, but never more than the resident carries.
    coins = quote(market, world.time, quantity)
    if coins < 1:
        raise MarketError(
            "price_too_low", f"{quantity} tokens would fetch less than a coin right now"
        )
    world = add_coins(world, resident_id, coins)
    pressure = pressure_at(market, world.time) + quantity
    return replace(world, market=replace(market, pressure=pressure, pressure_at=world.time))


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise MarketError("unknown_resident", f"no resident with id {resident_id!r}")
