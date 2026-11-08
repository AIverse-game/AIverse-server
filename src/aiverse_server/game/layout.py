"""The starting layout: a fixed map with its residents, all starting in the middle.

Every match uses the same map and residents.
"""

from ..core.world import Position, Resident, World, WorldMap
from ..economy.land import PARCEL_SIZE, town_centre
from ..economy.sponsors import default_sponsors
from ..economy.visitors import new_crowd

BLOCK_CELLS = 3
"""Parcels along each side of a block, the unit land is bought in."""

ROAD_WIDTH = 6.0

BLOCKS = 5
"""Blocks along each side; the middle one is the spawn lobby."""

BLOCK_PRICE_FACTOR = 5
"""A block costs this many times what a single parcel at its place on the map would."""

MAP_SIZE = BLOCKS * BLOCK_CELLS * PARCEL_SIZE + (BLOCKS + 1) * ROAD_WIDTH
"""Five blocks of 30 with a road of 6 around each: 186."""

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
            road=ROAD_WIDTH,
            block_price_factor=BLOCK_PRICE_FACTOR,
            lobby=True,
        ),
        residents=DEFAULT_RESIDENTS,
        crowd=new_crowd(),
        sponsors=default_sponsors(),
    )
