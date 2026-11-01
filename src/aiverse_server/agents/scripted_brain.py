"""A brain with a fixed plan, as a baseline and a stand-in opponent that costs nothing.

It buys the best chip it can afford, walks to the nearest pool it knows that still holds
tokens, exploring a fixed set of spots until it finds one, and mines there until it carries
`SELL_AT` tokens. Then it walks back to the market and sells all but `KEEP_TOKENS`. When the
game is about to end it heads for the market and sells all but one token, staying in the
game. It never talks or trades.
"""

import random

from ..core.world import Position
from ..economy.tokens import walk_cost
from .observation import Observation
from .tools import ResidentTools

SELL_AT = 260
"""Carried tokens at which the bot goes to sell."""

KEEP_TOKENS = 60
"""Tokens kept after selling, to walk back out to a pool."""

END_MARGIN = 20.0
"""Seconds before the end, beyond the walk, to be back at the market."""

EXPLORE = (
    (37.5, 37.5),
    (112.5, 37.5),
    (112.5, 112.5),
    (37.5, 112.5),
    (75.0, 18.0),
    (132.0, 75.0),
    (75.0, 132.0),
    (18.0, 75.0),
)
"""Spots to look around from, on a map of size 150, until a pool is found."""


class ScriptedBrain:
    def __init__(self, seed: int | None = None) -> None:
        self._spots = list(EXPLORE)
        random.Random(seed).shuffle(self._spots)
        self._empty: set[str] = set()
        """Pools found empty when the bot tried to mine them."""

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        me = observation.me
        market = observation.market
        if me.walking_to is not None or me.mining_pool is not None or market is None:
            return None
        tokens = me.tokens
        endgame = observation.remaining_seconds < market.walk_seconds + END_MARGIN
        if me.at_market:
            keep = 1 if endgame else KEEP_TOKENS
            if (endgame or tokens >= SELL_AT) and tokens > keep:
                result = tools.sell_tokens(tokens - keep)
                if result.ok and result.status is not None:
                    tokens = result.status["tokens"]
            if endgame:
                return "sold up for the end of the game"
            if not me.chips:
                affordable = [chip for chip in market.chips if chip.price <= me.coins]
                if not affordable:
                    return "cannot afford a chip"
                best = max(affordable, key=lambda chip: chip.tokens_per_job)
                if not tools.buy_chip(best.model).ok:
                    return "could not buy a chip"
        elif endgame or not me.chips or tokens >= SELL_AT:
            # A walk that spends the last token ends the game for the walker on arrival.
            if market.walk_cost >= tokens:
                return "too few tokens to reach the market"
            tools.move_to(market.position.x, market.position.y)
            return "going to the market"
        pools = [
            pool
            for pool in observation.known_pools
            if pool.reserve > 0 and pool.id not in self._empty
        ]
        if me.at_pool is not None and any(pool.id == me.at_pool for pool in pools):
            result = tools.mine(me.at_pool)
            if result.ok:
                return f"mining {me.at_pool}"
            if result.code == "pool_empty":
                self._empty.add(me.at_pool)
                pools = [pool for pool in pools if pool.id != me.at_pool]
        reachable = [pool for pool in pools if pool.walk_cost < tokens - 10]
        if reachable:
            pool = min(reachable, key=lambda pool: pool.distance)
            tools.move_to(pool.position.x, pool.position.y)
            return f"going to {pool.id}"
        while self._spots:
            x, y = self._spots.pop(0)
            if walk_cost(me.position, Position(x, y)) < tokens - 10:
                tools.move_to(x, y)
                return f"exploring ({x:g}, {y:g})"
        return "nothing left to do"
