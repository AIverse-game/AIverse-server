"""Land: square parcels residents buy from the town to build facilities on.

The map is cut into a grid of `PARCEL_SIZE` squares, counted by column and row from the
north-west corner. A resident standing inside a parcel nobody owns can buy it for coins; a
parcel that overlaps a spring, a mine, the studio or the market cannot be bought, so land never
blocks the places everyone shares. Land costs more the nearer it lies to the market at the
centre, where visitors gather, see `facilities.py`. Parcels stay their owner's for the whole
game, even after the owner leaves; nothing is paid back for them at the end.

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
"""Coins a parcel far from the market costs."""

LAND_PRICE_CENTRE = 3_000
"""Coins a parcel right next to the market would cost."""

LAND_PRICE_REACH = 75.0
"""Distance from the market, parcel centre to market centre, beyond which land costs
`LAND_PRICE_EDGE`; nearer, the price rises in a straight line towards `LAND_PRICE_CENTRE`."""

LandErrorCode = Literal["unknown_resident", "parcel_reserved", "parcel_owned"]


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
    reserved: bool
    """It overlaps a place everyone shares and cannot be bought."""
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
    return replace(_layout(*_layout_key(world))[row * grid_size(world) + col], owner_id=owner)


def plots(world: World) -> tuple[Plot, ...]:
    """Every grid square, row by row from the north-west corner."""
    owners = {(parcel.col, parcel.row): parcel.owner_id for parcel in world.parcels}
    return tuple(
        replace(square, owner_id=owners[(square.col, square.row)])
        if (square.col, square.row) in owners
        else square
        for square in _layout(*_layout_key(world))
    )


def land_price(world: World, centre: Position) -> int:
    """Coins a parcel centred at `centre` costs, rounded to tens."""
    return _price(centre, market_centre(world))


def _price(centre: Position, market: Position) -> int:
    nearness = max(1 - _distance(centre, market) / LAND_PRICE_REACH, 0.0)
    price = LAND_PRICE_EDGE + (LAND_PRICE_CENTRE - LAND_PRICE_EDGE) * nearness
    return int(round(price / 10) * 10)


def market_centre(world: World) -> Position:
    """Where visitors gather: the market, or the middle of a map without one."""
    if world.market is not None:
        return world.market.position
    return Position(world.map.size / 2, world.map.size / 2)


def buy_parcel(world: World, resident_id: str) -> World:
    """Buy the parcel the resident stands in from the town; the caller checks they may act
    and are not busy, see `actions.perform`."""
    resident = next((r for r in world.residents if r.id == resident_id), None)
    if resident is None:
        raise LandError("unknown_resident", f"no resident with id {resident_id!r}")
    square = plot_at(world, resident.position)
    if square.reserved:
        raise LandError(
            "parcel_reserved",
            f"parcel {square.id} overlaps a spring, mine, the studio or the market",
        )
    if square.owner_id is not None:
        raise LandError("parcel_owned", f"parcel {square.id} already belongs to {square.owner_id}")
    world = take_coins(world, resident_id, square.price)
    world = record_transfer(world, resident_id, SYSTEM, square.price, "land")
    return replace(world, parcels=(*world.parcels, Parcel(square.col, square.row, resident_id)))


def _layout_key(
    world: World,
) -> tuple[float, Position, tuple[tuple[float, float, float], ...]]:
    """What the grid's prices and reserved squares depend on, none of which moves in a game."""
    places = (*world.springs, *world.mines, world.studio, world.market)
    shapes = tuple(
        (place.position.x, place.position.y, place.radius) for place in places if place is not None
    )
    return world.map.size, market_centre(world), shapes


@functools.lru_cache(maxsize=8)
def _layout(
    size: float, market: Position, shapes: tuple[tuple[float, float, float], ...]
) -> tuple[Plot, ...]:
    """Every grid square without owners, row by row; worked out once per map, since every
    observation asks for it."""
    count = max(int(size // PARCEL_SIZE), 1)
    squares = []
    for row in range(count):
        for col in range(count):
            left, top = col * PARCEL_SIZE, row * PARCEL_SIZE
            right = size if col == count - 1 else left + PARCEL_SIZE
            bottom = size if row == count - 1 else top + PARCEL_SIZE
            centre = Position((left + right) / 2, (top + bottom) / 2)
            reserved = any(
                _overlaps(Position(x, y), radius, (left, top, right, bottom))
                for x, y, radius in shapes
            )
            squares.append(Plot(col, row, centre, _price(centre, market), reserved, None))
    return tuple(squares)


def _overlaps(centre: Position, radius: float, bounds: tuple[float, float, float, float]) -> bool:
    """Whether a circle reaches into a rectangle, edges touching included."""
    left, top, right, bottom = bounds
    nearest_x = min(max(centre.x, left), right)
    nearest_y = min(max(centre.y, top), bottom)
    return _distance(centre, Position(nearest_x, nearest_y)) <= radius


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
