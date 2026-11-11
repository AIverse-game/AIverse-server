"""The starting layout: a fixed map with its residents, all starting in the middle.

Every match uses the same map and residents.
"""

from ..core.world import Position, Resident, World, WorldMap
from ..economy.land import PARCEL_SIZE, town_centre
from ..economy.sponsors import default_sponsors
from ..economy.visitors import MAX_VISITORS, new_crowd

BLOCK_CELLS = 3
"""Parcels along each side of a block, the unit land is bought in."""

VENUE_CELLS = 2
"""Parcels along each side of the square a game hall or a workspace takes up: 2 x 2 parcels,
20 x 20 map units."""

BLOCKS = 5
"""Blocks along each side; the middle one is the spawn lobby."""

BLOCK_PRICE_FACTOR = 4
"""A block costs this many times what a single parcel at its place on the map would."""

MAP_SIZE = BLOCKS * BLOCK_CELLS * PARCEL_SIZE
"""Five blocks of 30 side by side: 150."""

STARTING_COINS = 10_000


def town_position(size: float = MAP_SIZE) -> Position:
    """The centre of the map, where every resident starts."""
    return town_centre(size)


def _resident(id: str, name: str, color: str, position: Position) -> Resident:
    return Resident(id=id, name=name, color=color, position=position, coins=STARTING_COINS)


DEFAULT_RESIDENTS = (
    _resident("r1", "阿青", "#e4572e", town_position()),
    _resident("r2", "小麦", "#f3a712", town_position()),
    _resident("r3", "松木", "#5fb49c", town_position()),
    _resident("r4", "白露", "#8e9aed", town_position()),
    _resident("r5", "石榴", "#ff99c8", town_position()),
    _resident("r6", "云杉", "#a0c4ff", town_position()),
    _resident("r7", "秋水", "#c9a27e", town_position()),
    _resident("r8", "柚子", "#e5e5e5", town_position()),
)


def create_default_world() -> World:
    return World(
        map=WorldMap(
            size=MAP_SIZE,
            block_cells=BLOCK_CELLS,
            block_price_factor=BLOCK_PRICE_FACTOR,
            lobby=True,
            venue_cells=VENUE_CELLS,
        ),
        residents=DEFAULT_RESIDENTS,
        crowd=new_crowd(MAX_VISITORS),
        sponsors=default_sponsors(),
    )
