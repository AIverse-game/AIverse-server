"""A brain with a fixed plan, as a baseline and a stand-in opponent that costs nothing.

It buys the best crystal it can afford, walks to the nearest spring it knows that still holds
energy, exploring a fixed set of spots until it finds one, and harvests there until it carries
`SELL_AT` energy. Then it walks back to the market and sells all but `KEEP_ENERGY`. When the
game is about to end it heads for the market and sells all but 1 energy, staying in the
game. It never talks or trades.
"""

import random

from ..core.world import Position
from ..economy.energy import walk_cost
from .observation import Observation
from .tools import ResidentTools

SELL_AT = 260
"""Carried energy at which the bot goes to sell."""

KEEP_ENERGY = 60
"""Energy kept after selling, to walk back out to a spring."""

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
"""Spots to look around from, on a map of size 150, until a spring is found."""


class ScriptedBrain:
    def __init__(self, seed: int | None = None) -> None:
        self._spots = list(EXPLORE)
        random.Random(seed).shuffle(self._spots)
        self._empty: set[str] = set()
        """Springs found empty when the bot tried to harvest them."""

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        me = observation.me
        market = observation.market
        if me.walking_to is not None or me.harvesting_spring is not None or market is None:
            return None
        energy = me.energy
        endgame = observation.remaining_seconds < market.walk_seconds + END_MARGIN
        if me.at_market:
            keep = 1 if endgame else KEEP_ENERGY
            if (endgame or energy >= SELL_AT) and energy > keep:
                result = tools.sell_energy(energy - keep)
                if result.ok and result.status is not None:
                    energy = result.status["energy"]
            if endgame:
                return "sold up for the end of the game"
            if not me.crystals:
                affordable = [crystal for crystal in market.crystals if crystal.price <= me.coins]
                if not affordable:
                    return "cannot afford a crystal"
                best = max(affordable, key=lambda crystal: crystal.energy_per_job)
                if not tools.buy_crystal(best.tier).ok:
                    return "could not buy a crystal"
        elif endgame or not me.crystals or energy >= SELL_AT:
            # A walk that spends the last energy ends the game for the walker on arrival.
            if market.walk_cost >= energy:
                return "too little energy to reach the market"
            tools.move_to(market.position.x, market.position.y)
            return "going to the market"
        springs = [
            spring
            for spring in observation.known_springs
            if spring.reserve > 0 and spring.id not in self._empty
        ]
        if me.at_spring is not None and any(spring.id == me.at_spring for spring in springs):
            result = tools.harvest(me.at_spring)
            if result.ok:
                return f"harvesting {me.at_spring}"
            if result.code == "spring_empty":
                self._empty.add(me.at_spring)
                springs = [spring for spring in springs if spring.id != me.at_spring]
        reachable = [spring for spring in springs if spring.walk_cost < energy - 10]
        if reachable:
            spring = min(reachable, key=lambda spring: spring.distance)
            tools.move_to(spring.position.x, spring.position.y)
            return f"going to {spring.id}"
        while self._spots:
            x, y = self._spots.pop(0)
            if walk_cost(me.position, Position(x, y)) < energy - 10:
                tools.move_to(x, y)
                return f"exploring ({x:g}, {y:g})"
        return "nothing left to do"
