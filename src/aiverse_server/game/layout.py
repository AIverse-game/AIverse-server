"""The starting layout: a fixed map, residents and market, with springs, quarries and the
workshop placed at random.

Every match uses the same map, residents and market, which stands in the middle of the map.
The springs, then the quarries, then the workshop are placed when the world is created, from a
seed, and stay where they are for the whole game; the quarries and workshop come after, so a
seed puts the springs where it always did.

Spring and quarry reserves grow with the number of residents, so each resident has the same
share to draw on however many play. With four residents a seed gives the world it did before
reserves grew with the count; the default eight residents widen the reserve ranges, which
changes how the seed's random numbers fall, so a seed lays out a different world than it did
with four.
"""

import math
import random

from ..core.world import (
    Inventory,
    Market,
    Position,
    PricePoint,
    Quarry,
    Resident,
    Spring,
    Workshop,
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
SPRING_RESERVE_PER_RESIDENT = (375, 1_000)
"""Smallest and largest number of energy a spring starts with, per resident in the game."""
SPRING_MARGIN = 15.0
"""Least distance from a spring's centre to the map edge."""
SPRING_SPACING = 30.0
"""Least distance between two springs' centres, so they do not overlap."""
MARKET_CLEARANCE = 30.0
"""Least distance from a spring's centre to the market's, so selling takes a walk from
harvesting."""

QUARRY_COUNT = 3
QUARRY_RADIUS = 4.0
QUARRY_RESERVE_PER_RESIDENT = (75, 150)
"""Smallest and largest number of shards a quarry starts with, per resident in the game."""
WORKSHOP_RADIUS = 5.0
PLACE_SPACING = 20.0
"""Least distance from a quarry's or the workshop's centre to any spring, quarry or workshop
placed before it."""

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
    _resident("r5", "石榴", "#ff99c8", market_position()),
    _resident("r6", "云杉", "#a0c4ff", market_position()),
    _resident("r7", "秋水", "#c9a27e", market_position()),
    _resident("r8", "柚子", "#e5e5e5", market_position()),
)


def spring_reserve(residents: int) -> tuple[int, int]:
    """Smallest and largest reserve a spring starts with in a game of `residents`."""
    low, high = SPRING_RESERVE_PER_RESIDENT
    return low * residents, high * residents


def quarry_reserve(residents: int) -> tuple[int, int]:
    """Smallest and largest reserve a quarry starts with in a game of `residents`."""
    low, high = QUARRY_RESERVE_PER_RESIDENT
    return low * residents, high * residents


def scatter_springs(
    seed: int | None = None, size: float = MAP_SIZE, residents: int = len(DEFAULT_RESIDENTS)
) -> tuple[Spring, ...]:
    """`SPRING_COUNT` springs at random spots at least `SPRING_SPACING` apart and `MARKET_CLEARANCE`
    from the market, each with a random reserve for a game of `residents`. The same seed gives
    the same springs; without one, every call differs."""
    return _scatter_springs(random.Random(seed), size, residents)


def _scatter_springs(rng: random.Random, size: float, residents: int) -> tuple[Spring, ...]:
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
            reserve = rng.randint(*spring_reserve(residents))
            springs.append(Spring(f"s{len(springs) + 1}", position, SPRING_RADIUS, reserve))
    if len(springs) < SPRING_COUNT:
        raise ValueError(f"cannot fit {SPRING_COUNT} springs on a map of size {size:g}")
    return tuple(springs)


def _place(rng: random.Random, size: float, taken: list[Position]) -> Position:
    """A random spot `MARKET_CLEARANCE` from the market and `PLACE_SPACING` from every spot
    in `taken`, as far from the edge as springs are."""
    market = market_position(size)
    for _ in range(10_000):
        position = Position(
            round(rng.uniform(SPRING_MARGIN, size - SPRING_MARGIN), 1),
            round(rng.uniform(SPRING_MARGIN, size - SPRING_MARGIN), 1),
        )
        if math.dist((market.x, market.y), (position.x, position.y)) >= MARKET_CLEARANCE and all(
            math.dist((other.x, other.y), (position.x, position.y)) >= PLACE_SPACING
            for other in taken
        ):
            return position
    raise ValueError(f"cannot fit the quarries and workshop on a map of size {size:g}")


def create_default_world(seed: int | None = None) -> World:
    rng = random.Random(seed)
    residents = len(DEFAULT_RESIDENTS)
    springs = _scatter_springs(rng, MAP_SIZE, residents)
    taken = [spring.position for spring in springs]
    quarries: list[Quarry] = []
    for number in range(1, QUARRY_COUNT + 1):
        position = _place(rng, MAP_SIZE, taken)
        taken.append(position)
        reserve = rng.randint(*quarry_reserve(residents))
        quarries.append(Quarry(f"q{number}", position, QUARRY_RADIUS, reserve))
    return World(
        map=WorldMap(size=MAP_SIZE),
        residents=DEFAULT_RESIDENTS,
        springs=springs,
        market=Market(
            market_position(), MARKET_RADIUS, history=(PricePoint(0.0, MARKET_BASE_PRICE),)
        ),
        quarries=tuple(quarries),
        workshop=Workshop(_place(rng, MAP_SIZE, taken), WORKSHOP_RADIUS),
    )
