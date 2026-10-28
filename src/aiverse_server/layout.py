"""The starting layout: a fixed map and residents, with token pools scattered at random.

Every match uses the same map and residents. The pools are placed when the world is created,
from a seed, and stay where they are for the whole game.
"""

import math
import random

from .tokens import TOKEN
from .world import Inventory, Pool, Position, Resident, World, WorldMap

MAP_SIZE = 100.0

STARTING_COINS = 10_000

STARTING_TOKENS = 200

POOL_COUNT = 6
POOL_RADIUS = 4.0
POOL_RESERVE = (150, 400)
"""Smallest and largest number of tokens a pool starts with."""
POOL_MARGIN = 10.0
"""Least distance from a pool's centre to the map edge."""
POOL_SPACING = 15.0
"""Least distance between two pools' centres, so they do not overlap."""

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
    """`POOL_COUNT` pools at random spots at least `POOL_SPACING` apart, each with a random
    reserve. The same seed gives the same pools; without one, every call differs."""
    rng = random.Random(seed)
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
        if all(
            math.dist((p.position.x, p.position.y), (position.x, position.y)) >= POOL_SPACING
            for p in pools
        ):
            reserve = rng.randint(*POOL_RESERVE)
            pools.append(Pool(f"p{len(pools) + 1}", position, POOL_RADIUS, reserve))
    if len(pools) < POOL_COUNT:
        raise ValueError(f"cannot fit {POOL_COUNT} pools on a map of size {size:g}")
    return tuple(pools)


def create_default_world(seed: int | None = None) -> World:
    return World(
        map=WorldMap(size=MAP_SIZE), residents=DEFAULT_RESIDENTS, pools=scatter_pools(seed)
    )
