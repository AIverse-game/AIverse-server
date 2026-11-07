"""A brain with a fixed plan, as a baseline and a stand-in opponent that costs nothing.

It walks to the next spot in a fixed tour, says hello there and, the first time it stands in a
free parcel it can afford, buys it. Then it walks on, and once the tour is done it waits. It
never trades.
"""

from ..core.world import Position
from .observation import Observation
from .tools import ResidentTools

TOUR = (
    (37.5, 37.5),
    (112.5, 37.5),
    (112.5, 112.5),
    (37.5, 112.5),
)
"""Spots to visit, on a map of size 150, in order."""


class ScriptedBrain:
    def __init__(self) -> None:
        self._tour = list(TOUR)
        self._bought = False

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        me = observation.me
        if me.walking_to is not None:
            return None
        here = observation.land.here
        if not self._bought and here is not None and here.owner is None and here.price <= me.coins:
            self._bought = tools.buy_parcel().ok
            if self._bought:
                return f"bought {here.id}"
        if self._tour:
            x, y = self._tour.pop(0)
            if Position(x, y) != me.position:
                tools.say("Hello!")
                tools.move_to(x, y)
                return f"visiting ({x:g}, {y:g})"
        return "nothing left to do"
