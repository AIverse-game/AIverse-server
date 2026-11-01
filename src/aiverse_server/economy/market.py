"""The market: a buyer from outside the town who pays coins for tokens.

Apart from the coins residents start with, this is where new coins come from: trades
between residents only move coins around. A resident standing within the market's radius
sells tokens they carry with `sell_tokens` and is paid at the market price.

The price falls as tokens are sold and rises as time passes. The market keeps a pressure,
the tokens sold recently, whose distance from `MARKET_RESTING_PRESSURE` halves every
`MARKET_HALF_LIFE` seconds of game time. The price of one more token at pressure S is
`MARKET_BASE_PRICE * MARKET_DEPTH / (MARKET_DEPTH + S)`, so a pressure of `MARKET_DEPTH`
halves it. The resting pressure is below zero, so a market nobody sells to for a long while
pays more than the base price, up to `MARKET_TOP_PRICE`. Selling q tokens pays that
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
"""Coins the market pays for a token at zero pressure, as when the game begins."""

MARKET_DEPTH = 400.0
"""Recent sales, in tokens, that halve the price."""

MARKET_TOP_PRICE = 40.0
"""Coins the market pays for a token after nobody has sold for a very long while."""

MARKET_RESTING_PRESSURE = -MARKET_DEPTH * (1 - MARKET_BASE_PRICE / MARKET_TOP_PRICE)
"""Pressure the market drifts to with no sales, where a token fetches `MARKET_TOP_PRICE`."""

MARKET_HALF_LIFE = 60.0
"""Game seconds for the pressure's distance from `MARKET_RESTING_PRESSURE` to halve."""

MARKET_RADIUS = 5.0

MarketErrorCode = Literal["unknown_resident", "not_at_market", "invalid_quantity", "price_too_low"]


class MarketError(Exception):
    def __init__(self, code: MarketErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: MarketErrorCode = code
        self.message = message


def pressure_at(market: Market, time: float) -> float:
    """Recent sales in tokens at game time `time`, drifted towards the resting pressure since
    the last sale; below zero once the market has gone without sales for a while."""
    elapsed = max(time - market.pressure_at, 0.0)
    drift = math.exp2(-elapsed / MARKET_HALF_LIFE)
    return MARKET_RESTING_PRESSURE + (market.pressure - MARKET_RESTING_PRESSURE) * drift


def unit_price(market: Market, time: float) -> float:
    """Coins one more token would fetch at game time `time`, before rounding."""
    return MARKET_BASE_PRICE * MARKET_DEPTH / (MARKET_DEPTH + pressure_at(market, time))


def quote(market: Market, time: float, quantity: int, seen: float | None = None) -> int:
    """Whole coins selling `quantity` tokens at game time `time` pays. `seen` is the pressure
    the seller saw earlier, if any: sales by others since then do not lower the price."""
    pressure = pressure_at(market, time)
    if seen is not None:
        pressure = min(pressure, seen)
    depth = MARKET_DEPTH + pressure
    earned = MARKET_BASE_PRICE * MARKET_DEPTH * math.log1p(quantity / depth)
    # The slack keeps float noise just under a whole number from costing a coin.
    return math.floor(earned + 1e-9)


def sell_tokens(world: World, resident_id: str, quantity: int, seen: float | None = None) -> World:
    """Sell `quantity` tokens the resident carries to the market they stand at, for coins,
    priced from the pressure `seen` earlier if the market has fallen since, see `quote`. The
    market is left with its real pressure plus this sale either way."""
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
    coins = quote(market, world.time, quantity, seen)
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
