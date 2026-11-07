"""A brain with a fixed plan, as a baseline and a stand-in opponent that costs nothing.

It walks to the next spot in a fixed tour, says hello there and, the first time it stands in a
free parcel it can afford, buys it and starts an ad slot there on its own compute. Whenever its
own compute is free and it owns an ad slot or is building one, it renders content on it and
puts the content it carries into the slot. Once the tour is done it waits. It never trades.
"""

from ..core.world import Position
from ..economy.ads import CONTENT_ITEM
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
        self._tend_slot(observation, tools)
        if me.walking_to is not None:
            return None
        here = observation.land.here
        if not self._bought and here is not None and here.owner is None and here.price <= me.coins:
            self._bought = tools.buy_parcel().ok
            if self._bought:
                tools.build_ad(here.id, "self")
                return f"bought {here.id} and started an ad slot"
        if self._tour:
            x, y = self._tour.pop(0)
            if Position(x, y) != me.position:
                tools.say("Hello!")
                tools.move_to(x, y)
                return f"visiting ({x:g}, {y:g})"
        return "nothing left to do"

    def _tend_slot(self, observation: Observation, tools: ResidentTools) -> None:
        """Keep content flowing into its ad slot: show what it carries, and start the next
        piece on its own compute when that is free."""
        land, compute = observation.land, observation.compute
        slots = [parcel for parcel in land.mine if parcel.facility == "ad"]
        if slots and observation.me.items.get(CONTENT_ITEM):
            roomy = min(slots, key=lambda parcel: len(parcel.content_goes_stale_in))
            if len(roomy.content_goes_stale_in) < land.slot_capacity:
                tools.place_content(roomy.id)
        if land.mine and compute.own_job is None:
            tools.make_content("self")
