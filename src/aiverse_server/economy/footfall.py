"""Footfall: how many visitors pass a spot on the map.

Visitors are not simulated one by one. Their number grows as the game goes on, `visitors`, and
they crowd towards the centre of the map, `footfall`, fewer the farther out a spot lies;
residents standing nearby add to the crowd. Ad slots showing content are paid by it, see
`ads.py`.
"""

import math

from ..core.world import Position, World
from .land import town_centre

VISITORS_AT_START = 100.0
"""Footfall at the centre when the game starts."""

VISITOR_GROWTH_SECONDS = 1_200.0
"""Game seconds over which the visitors grow by another `VISITORS_AT_START`."""

FOOTFALL_REACH = 75.0
"""Distance from the centre at which the share of visitors passing has fallen, in a straight
line, from all of them to `FOOTFALL_FLOOR`; it stays there farther out."""

FOOTFALL_FLOOR = 0.25
"""Share of the visitors that pass even the farthest spot."""

RESIDENT_FOOTFALL = 10.0
"""Footfall each resident standing within `RESIDENT_REACH` of a spot adds."""

RESIDENT_REACH = 15.0


def visitors(time: float) -> float:
    """Visitors at the centre at game time `time`; they grow in a straight line."""
    return VISITORS_AT_START * (1 + time / VISITOR_GROWTH_SECONDS)


def footfall(world: World, centre: Position) -> float:
    """Visitors and residents passing a spot at `centre` right now."""
    distance = _distance(centre, town_centre(world.map.size))
    share = 1 - (1 - FOOTFALL_FLOOR) * min(distance / FOOTFALL_REACH, 1.0)
    standing = sum(
        1
        for resident in world.residents
        if not resident.left
        and resident.walking is None
        and _distance(resident.position, centre) <= RESIDENT_REACH
    )
    return visitors(world.time) * share + RESIDENT_FOOTFALL * standing


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
