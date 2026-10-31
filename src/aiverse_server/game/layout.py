"""The starting layout: a fixed map, residents and market, with token pools scattered at random.

Every match uses the same map, residents and market, which stands in the middle of the map.
The pools are placed when the world is created, from a seed, and stay where they are for the
whole game.
"""

import math
import random

from ..core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from ..economy.market import MARKET_RADIUS
from ..economy.tokens import TOKEN

MAP_SIZE = 100.0

STARTING_COINS = 10_000

STARTING_TOKENS = 200

POOL_COUNT = 6
POOL_RADIUS = 4.0
POOL_RESERVE = (1_500, 4_000)
"""Smallest and largest number of tokens a pool starts with."""
POOL_MARGIN = 10.0
"""Least distance from a pool's centre to the map edge."""
POOL_SPACING = 15.0
"""Least distance between two pools' centres, so they do not overlap."""
MARKET_CLEARANCE = 20.0
"""Least distance from a pool's centre to the market's, so selling takes a walk from mining."""

_STARTING_INVENTORY = Inventory(((TOKEN, STARTING_TOKENS),))


def _resident(id: str, name: str, color: str, x: float, y: float) -> Resident:
    return Resident(
        id=id,
        name=name,
        color=color,
        position=Position(x, y),
        inventory=_STARTING_INVENTORY,
        coins=STARTING_COINS,
    )


# Demo residents; spread out so the coordinate system is easy to check on screen.
DEFAULT_RESIDENTS = (
    _resident("r1", "阿青", "#e4572e", 20, 25),
    _resident("r2", "小麦", "#f3a712", 72, 18),
    _resident("r3", "松木", "#5fb49c", 35, 70),
    _resident("r4", "白露", "#8e9aed", 82, 84),
)


def scatter_pools(seed: int | None = None, size: float = MAP_SIZE) -> tuple[Pool, ...]:
    """`POOL_COUNT` pools at random spots at least `POOL_SPACING` apart and `MARKET_CLEARANCE`
    from the market, each with a random reserve. The same seed gives the same pools; without
    one, every call differs."""
    rng = random.Random(seed)
    market = market_position(size)
    pools: list[Pool] = []
    # The spacing leaves plenty of room on the default map; the bound only guards a
    # misconfigured one from looping forever.
    for _ in range(10_000):
        if len(pools) == POOL_COUNT:
            break
        position = Position(
            round(rng.uniform(POOL_MARGIN, size - POOL_MARGIN), 1),
            round(rng.uniform(POOL_MARGIN, size - POOL_MARGIN), 1),
        )
        if math.dist((market.x, market.y), (position.x, position.y)) >= MARKET_CLEARANCE and all(
            math.dist((p.position.x, p.position.y), (position.x, position.y)) >= POOL_SPACING
            for p in pools
        ):
            reserve = rng.randint(*POOL_RESERVE)
            pools.append(Pool(f"p{len(pools) + 1}", position, POOL_RADIUS, reserve))
    if len(pools) < POOL_COUNT:
        raise ValueError(f"cannot fit {POOL_COUNT} pools on a map of size {size:g}")
    return tuple(pools)


def market_position(size: float = MAP_SIZE) -> Position:
    """The centre of the map."""
    return Position(size / 2, size / 2)


def create_default_world(seed: int | None = None) -> World:
    return World(
        map=WorldMap(size=MAP_SIZE),
        residents=DEFAULT_RESIDENTS,
        pools=scatter_pools(seed),
        market=Market(market_position(), MARKET_RADIUS),
    )
