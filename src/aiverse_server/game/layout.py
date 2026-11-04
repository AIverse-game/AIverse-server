"""The starting layout: a fixed map, residents and market, with springs scattered at random.

Every match uses the same map, residents and market, which stands in the middle of the map.
The springs are placed when the world is created, from a seed, and stay where they are for the
whole game.
"""

import math
import random

from ..core.world import (
    Inventory,
    Market,
    Position,
    PricePoint,
    Resident,
    Spring,
    World,
    WorldMap,
)
from ..economy.energy import ENERGY
from ..economy.market import MARKET_BASE_PRICE, MARKET_RADIUS

MAP_SIZE = 150.0

STARTING_COINS = 10_000

STARTING_ENERGY = 200

SPRING_COUNT = 6
SPRING_RADIUS = 4.0
SPRING_RESERVE = (1_500, 4_000)
"""Smallest and largest number of energy a spring starts with."""
SPRING_MARGIN = 15.0
"""Least distance from a spring's centre to the map edge."""
SPRING_SPACING = 30.0
"""Least distance between two springs' centres, so they do not overlap."""
MARKET_CLEARANCE = 30.0
"""Least distance from a spring's centre to the market's, so selling takes a walk from
harvesting."""

_STARTING_INVENTORY = Inventory(((ENERGY, STARTING_ENERGY),))


def market_position(size: float = MAP_SIZE) -> Position:
    """The centre of the map."""
    return Position(size / 2, size / 2)


def _resident(id: str, name: str, color: str, position: Position) -> Resident:
    return Resident(
        id=id,
        name=name,
        color=color,
        position=position,
        inventory=_STARTING_INVENTORY,
        coins=STARTING_COINS,
    )


# All residents begin at the market so they can buy a crystal before heading to a spring.
DEFAULT_RESIDENTS = (
    _resident("r1", "阿青", "#e4572e", market_position()),
    _resident("r2", "小麦", "#f3a712", market_position()),
    _resident("r3", "松木", "#5fb49c", market_position()),
    _resident("r4", "白露", "#8e9aed", market_position()),
)


def scatter_springs(seed: int | None = None, size: float = MAP_SIZE) -> tuple[Spring, ...]:
    """`SPRING_COUNT` springs at random spots at least `SPRING_SPACING` apart and `MARKET_CLEARANCE`
    from the market, each with a random reserve. The same seed gives the same springs; without
    one, every call differs."""
    rng = random.Random(seed)
    market = market_position(size)
    springs: list[Spring] = []
    # The spacing leaves plenty of room on the default map; the bound only guards a
    # misconfigured one from looping forever.
    for _ in range(10_000):
        if len(springs) == SPRING_COUNT:
            break
        position = Position(
            round(rng.uniform(SPRING_MARGIN, size - SPRING_MARGIN), 1),
            round(rng.uniform(SPRING_MARGIN, size - SPRING_MARGIN), 1),
        )
        if math.dist((market.x, market.y), (position.x, position.y)) >= MARKET_CLEARANCE and all(
            math.dist((p.position.x, p.position.y), (position.x, position.y)) >= SPRING_SPACING
            for p in springs
        ):
            reserve = rng.randint(*SPRING_RESERVE)
            springs.append(Spring(f"s{len(springs) + 1}", position, SPRING_RADIUS, reserve))
    if len(springs) < SPRING_COUNT:
        raise ValueError(f"cannot fit {SPRING_COUNT} springs on a map of size {size:g}")
    return tuple(springs)


def create_default_world(seed: int | None = None) -> World:
    return World(
        map=WorldMap(size=MAP_SIZE),
        residents=DEFAULT_RESIDENTS,
        springs=scatter_springs(seed),
        market=Market(
            market_position(), MARKET_RADIUS, history=(PricePoint(0.0, MARKET_BASE_PRICE),)
        ),
    )
