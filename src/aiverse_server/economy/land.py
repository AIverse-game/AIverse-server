"""Land: square parcels residents buy from the town to build facilities on.

The map is cut into a grid of `PARCEL_SIZE` squares, counted by column and row from the
north-west corner. A resident standing inside a parcel nobody owns can buy it for coins. Land
costs more the nearer it lies to the centre of the map, where visitors gather, see
`footfall.py`. Parcels stay their owner's for the whole game, even after the owner leaves;
nothing is paid back for them at the end.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import functools
import math
from dataclasses import dataclass, replace
from typing import Literal

from ..core.inventory import take_coins
from ..core.land import Parcel, parcel_id
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import Position, World

PARCEL_SIZE = 10.0
"""Side length of a parcel, in map units."""

LAND_PRICE_EDGE = 800
"""Coins a parcel far from the centre costs."""

LAND_PRICE_CENTRE = 3_000
"""Coins a parcel right at the centre would cost."""

LAND_PRICE_REACH = 75.0
"""Distance from the centre of the map, to the parcel's centre, beyond which land costs
`LAND_PRICE_EDGE`; nearer, the price rises in a straight line towards `LAND_PRICE_CENTRE`."""

LandErrorCode = Literal["unknown_resident", "parcel_owned"]


class LandError(Exception):
    def __init__(self, code: LandErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: LandErrorCode = code
        self.message = message


@dataclass(frozen=True)
class Plot:
    """A grid square as the rules see it, owned or not."""

    col: int
    row: int
    centre: Position
    price: int
    owner_id: str | None

    @property
    def id(self) -> str:
        return parcel_id(self.col, self.row)


def grid_size(world: World) -> int:
    """Parcels along each side of the map; a strip narrower than a parcel at the east and
    south edges belongs to the last column and row."""
    return max(int(world.map.size // PARCEL_SIZE), 1)


def plot_at(world: World, position: Position) -> Plot:
    """The grid square `position` lies in."""
    last = grid_size(world) - 1
    col = min(int(position.x // PARCEL_SIZE), last)
    row = min(int(position.y // PARCEL_SIZE), last)
    return plot(world, col, row)


def plot(world: World, col: int, row: int) -> Plot:
    """The grid square at `col`, `row`, which must lie on the map."""
    if not 0 <= col < grid_size(world) or not 0 <= row < grid_size(world):
        raise ValueError(f"no parcel at column {col}, row {row}")
    owner = next((p.owner_id for p in world.parcels if (p.col, p.row) == (col, row)), None)
    return replace(_layout(world.map.size)[row * grid_size(world) + col], owner_id=owner)


def plots(world: World) -> tuple[Plot, ...]:
    """Every grid square, row by row from the north-west corner."""
    owners = {(parcel.col, parcel.row): parcel.owner_id for parcel in world.parcels}
    return tuple(
        replace(square, owner_id=owners[(square.col, square.row)])
        if (square.col, square.row) in owners
        else square
        for square in _layout(world.map.size)
    )


def land_price(world: World, centre: Position) -> int:
    """Coins a parcel centred at `centre` costs, rounded to tens."""
    return _price(centre, town_centre(world.map.size))


def _price(centre: Position, town: Position) -> int:
    nearness = max(1 - _distance(centre, town) / LAND_PRICE_REACH, 0.0)
    price = LAND_PRICE_EDGE + (LAND_PRICE_CENTRE - LAND_PRICE_EDGE) * nearness
    return int(round(price / 10) * 10)


def town_centre(size: float) -> Position:
    """Where visitors gather and residents start: the middle of a map of side `size`."""
    return Position(size / 2, size / 2)


def buy_parcel(world: World, resident_id: str) -> World:
    """Buy the parcel the resident stands in from the town; the caller checks they may act
    and are not busy, see `actions.perform`."""
    resident = next((r for r in world.residents if r.id == resident_id), None)
    if resident is None:
        raise LandError("unknown_resident", f"no resident with id {resident_id!r}")
    square = plot_at(world, resident.position)
    if square.owner_id is not None:
        raise LandError("parcel_owned", f"parcel {square.id} already belongs to {square.owner_id}")
    world = take_coins(world, resident_id, square.price)
    world = record_transfer(world, resident_id, SYSTEM, square.price, "land")
    return replace(world, parcels=(*world.parcels, Parcel(square.col, square.row, resident_id)))


@functools.lru_cache(maxsize=8)
def _layout(size: float) -> tuple[Plot, ...]:
    """Every grid square without owners, row by row; worked out once per map size, since every
    observation asks for it."""
    town = town_centre(size)
    count = max(int(size // PARCEL_SIZE), 1)
    squares = []
    for row in range(count):
        for col in range(count):
            left, top = col * PARCEL_SIZE, row * PARCEL_SIZE
            right = size if col == count - 1 else left + PARCEL_SIZE
            bottom = size if row == count - 1 else top + PARCEL_SIZE
            centre = Position((left + right) / 2, (top + bottom) / 2)
            squares.append(Plot(col, row, centre, _price(centre, town), None))
    return tuple(squares)


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
