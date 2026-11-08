"""Land: square parcels residents buy from the town to build facilities on.

The map is cut into a grid of `PARCEL_SIZE` squares, counted by column and row from the
north-west corner. Land is bought in blocks of `map.block_cells` x `map.block_cells` parcels,
which all become the buyer's at once; facilities are built on single parcels. Roads between
blocks, `map.road` wide, belong to no parcel. The spawn lobby in the middle of the map, if the
map has one, is a block nobody can buy. A resident standing inside a block nobody owns can buy
it for coins. Land costs more the nearer it lies to the centre of the map, where visitors come
in, see `visitors.py`. Parcels stay their owner's for the whole game, even after the owner
leaves; nothing is paid back for them at the end.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import bisect
import functools
import math
from dataclasses import dataclass, replace
from typing import Literal

from ..core.inventory import take_coins
from ..core.land import Parcel, parcel_id
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import Position, World, WorldMap

PARCEL_SIZE = 10.0
"""Side length of a parcel, in map units."""

LAND_PRICE_EDGE = 800
"""Coins a parcel far from the centre costs."""

LAND_PRICE_CENTRE = 3_000
"""Coins a parcel right at the centre would cost."""

LAND_PRICE_REACH = 75.0
"""Distance from the centre of the map, to the parcel's centre, beyond which land costs
`LAND_PRICE_EDGE`; nearer, the price rises in a straight line towards `LAND_PRICE_CENTRE`."""

LandErrorCode = Literal["unknown_resident", "parcel_owned", "not_for_sale"]


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
    """Coins the block this parcel lies in costs; every parcel of a block lists the same."""
    owner_id: str | None
    lobby: bool = False
    """Part of the spawn lobby, which is not for sale."""

    @property
    def id(self) -> str:
        return parcel_id(self.col, self.row)


@dataclass(frozen=True)
class _Axis:
    """Where the parcels lie along one side of the map."""

    starts: tuple[float, ...]
    ends: tuple[float, ...]

    def index(self, value: float) -> int:
        """The parcel whose span holds `value`, or the nearest one when it is on a road."""
        return min(max(bisect.bisect_right(self.starts, value) - 1, 0), len(self.starts) - 1)

    def on_road(self, value: float) -> bool:
        index = self.index(value)
        return not self.starts[index] <= value <= self.ends[index]


def grid_size(world: World) -> int:
    """Parcels along each side of the map; without roads, a strip narrower than a parcel at
    the east and south edges belongs to the last column and row."""
    return len(_axis(world.map).starts)


def plot_at(world: World, position: Position) -> Plot:
    """The grid square `position` lies in; on a road, the nearest one."""
    axis = _axis(world.map)
    return plot(world, axis.index(position.x), axis.index(position.y))


def on_road(world: World, position: Position) -> bool:
    """Whether `position` lies on a road, outside every parcel."""
    axis = _axis(world.map)
    return axis.on_road(position.x) or axis.on_road(position.y)


def block_of(world: World, col: int, row: int) -> tuple[tuple[int, int], ...]:
    """The (column, row) of every parcel in the block the parcel at `col`, `row` is in."""
    cells = world.map.block_cells
    first_col, first_row = col // cells * cells, row // cells * cells
    return tuple((first_col + dc, first_row + dr) for dr in range(cells) for dc in range(cells))


def plot(world: World, col: int, row: int) -> Plot:
    """The grid square at `col`, `row`, which must lie on the map."""
    count = grid_size(world)
    if not 0 <= col < count or not 0 <= row < count:
        raise ValueError(f"no parcel at column {col}, row {row}")
    owner = next((p.owner_id for p in world.parcels if (p.col, p.row) == (col, row)), None)
    return replace(_layout(world.map)[row * count + col], owner_id=owner)


def parcel_centre(world_map: WorldMap, col: int, row: int) -> Position:
    """Centre of the grid square at `col`, `row` on `world_map`."""
    count = len(_axis(world_map).starts)
    if not 0 <= col < count or not 0 <= row < count:
        raise ValueError(f"no parcel at column {col}, row {row}")
    return _layout(world_map)[row * count + col].centre


def plots(world: World) -> tuple[Plot, ...]:
    """Every grid square, row by row from the north-west corner."""
    owners = {(parcel.col, parcel.row): parcel.owner_id for parcel in world.parcels}
    return tuple(
        replace(square, owner_id=owners[(square.col, square.row)])
        if (square.col, square.row) in owners
        else square
        for square in _layout(world.map)
    )


def land_price(world: World, centre: Position) -> int:
    """Coins a parcel centred at `centre` costs, rounded to tens, before the block factor."""
    return _price(centre, town_centre(world.map.size))


def _price(centre: Position, town: Position) -> int:
    nearness = max(1 - _distance(centre, town) / LAND_PRICE_REACH, 0.0)
    price = LAND_PRICE_EDGE + (LAND_PRICE_CENTRE - LAND_PRICE_EDGE) * nearness
    return int(round(price / 10) * 10)


def town_centre(size: float) -> Position:
    """Where visitors gather and residents start: the middle of a map of side `size`."""
    return Position(size / 2, size / 2)


def buy_parcel(world: World, resident_id: str) -> World:
    """Buy the block the resident stands in from the town; the caller checks they may act
    and are not busy, see `actions.perform`."""
    resident = next((r for r in world.residents if r.id == resident_id), None)
    if resident is None:
        raise LandError("unknown_resident", f"no resident with id {resident_id!r}")
    square = plot_at(world, resident.position)
    check_for_sale(world, square, resident.position)
    world = take_coins(world, resident_id, square.price)
    world = record_transfer(world, resident_id, SYSTEM, square.price, "land")
    return add_block(world, square.col, square.row, resident_id)


def for_sale_at(world: World, position: Position) -> Plot | None:
    """The unowned, buyable land `position` stands on, or None on a road, in the lobby or on
    land that is owned."""
    square = plot_at(world, position)
    if square.lobby or square.owner_id is not None or on_road(world, position):
        return None
    return square


def check_for_sale(world: World, square: Plot, position: Position | None = None) -> None:
    """Raise `LandError` unless the block of `square` can be bought: it is not the lobby, not
    already owned, and `position`, if given, is not on a road."""
    if square.lobby or (position is not None and on_road(world, position)):
        raise LandError("not_for_sale", f"{square.id} is not land for sale")
    if square.owner_id is not None:
        raise LandError("parcel_owned", f"parcel {square.id} already belongs to {square.owner_id}")


def add_block(
    world: World,
    col: int,
    row: int,
    owner_id: str,
    shares: tuple[tuple[str, int], ...] = (),
) -> World:
    """Give every parcel of the block holding `col`, `row` to `owner_id`, holding `shares`
    (default: all of them)."""
    bought = tuple(Parcel(c, r, owner_id, shares=shares) for c, r in block_of(world, col, row))
    return replace(world, parcels=(*world.parcels, *bought))


@functools.lru_cache(maxsize=8)
def _axis(world_map: WorldMap) -> _Axis:
    cells, road = world_map.block_cells, world_map.road
    if cells == 1:
        count = max(int(world_map.size // PARCEL_SIZE), 1)
        starts = tuple(i * PARCEL_SIZE for i in range(count))
        ends = tuple(start + PARCEL_SIZE for start in starts[:-1]) + (world_map.size,)
        return _Axis(starts, ends)
    block = cells * PARCEL_SIZE
    blocks = int((world_map.size - road) // (block + road))
    if blocks < 1:
        raise ValueError(f"a map of side {world_map.size:g} has no room for a block")
    if world_map.lobby and blocks % 2 == 0:
        raise ValueError("a lobby needs an odd number of blocks along each side")
    starts = tuple(
        road + b * (block + road) + k * PARCEL_SIZE for b in range(blocks) for k in range(cells)
    )
    return _Axis(starts, tuple(start + PARCEL_SIZE for start in starts))


@functools.lru_cache(maxsize=8)
def _layout(world_map: WorldMap) -> tuple[Plot, ...]:
    """Every grid square without owners, row by row; worked out once per map, since every
    observation asks for it."""
    town = town_centre(world_map.size)
    axis = _axis(world_map)
    count = len(axis.starts)
    cells = world_map.block_cells
    middle = count // cells // 2
    squares = []
    for row in range(count):
        for col in range(count):
            centre = Position(
                (axis.starts[col] + axis.ends[col]) / 2, (axis.starts[row] + axis.ends[row]) / 2
            )
            first_col, first_row = col // cells * cells, row // cells * cells
            block_centre = Position(
                (axis.starts[first_col] + axis.ends[first_col + cells - 1]) / 2,
                (axis.starts[first_row] + axis.ends[first_row + cells - 1]) / 2,
            )
            price = _price(block_centre, town) * world_map.block_price_factor
            lobby = world_map.lobby and (col // cells, row // cells) == (middle, middle)
            squares.append(Plot(col, row, centre, price, None, lobby))
    return tuple(squares)


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
