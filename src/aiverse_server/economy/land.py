"""Land: square parcels residents buy from the town to build facilities on.

The map is cut into a grid of `PARCEL_SIZE` squares, counted by column and row from the
north-west corner. Land is bought in blocks of `map.block_cells` x `map.block_cells` parcels,
which all become the buyer's at once; facilities are built on single parcels. Blocks touch;
any roads between them are only drawn by the client and take no map units. The spawn lobby in
the middle of the map, if the map has one, is a block nobody can buy. A resident standing inside
a block nobody owns can buy it for coins. Land costs more the nearer it lies to the centre of
the map, where visitors come in, see `visitors.py`. Parcels stay their owner's for the whole
game, even after the owner leaves; nothing is paid back for them at the end.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import functools
import math
from dataclasses import dataclass, replace
from typing import Literal

from ..core.inventory import take_coins
from ..core.land import VENUE_FACILITIES, Parcel, parcel_id, parse_parcel_id
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


def block_of(world: World, col: int, row: int) -> tuple[tuple[int, int], ...]:
    """The (column, row) of every parcel in the block the parcel at `col`, `row` is in."""
    cells = world.map.block_cells
    first_col, first_row = col // cells * cells, row // cells * cells
    return tuple((first_col + dc, first_row + dr) for dr in range(cells) for dc in range(cells))


def block_parcels(world: World, parcel_id_: str) -> tuple[Parcel, ...]:
    """The parcels of the block holding the parcel `parcel_id_`, which someone owns; empty if
    the id names no parcel or the block is nobody's."""
    cell = parse_parcel_id(parcel_id_)
    if cell is None or not 0 <= max(cell) < grid_size(world):
        return ()
    cells = set(block_of(world, *cell))
    return tuple(p for p in world.parcels if (p.col, p.row) in cells)


def venue_footprint(world_map: WorldMap, col: int, row: int) -> tuple[tuple[int, int], ...]:
    """The (column, row) of every parcel a venue built on the parcel at `col`, `row` takes up:
    a square of `world_map.venue_cells` parcels with that parcel at its north-west corner."""
    cells = world_map.venue_cells
    return tuple((col + dc, row + dr) for dr in range(cells) for dc in range(cells))


def venue_fits(world_map: WorldMap, col: int, row: int) -> bool:
    """Whether a venue built on the parcel at `col`, `row` stays inside that parcel's block and
    on the map."""
    block, venue = world_map.block_cells, world_map.venue_cells
    count = max(int(world_map.size // PARCEL_SIZE), 1)
    return (
        col % block + venue <= block
        and row % block + venue <= block
        and col + venue <= count
        and row + venue <= count
    )


def venue_centre(world_map: WorldMap, col: int, row: int) -> Position:
    """The middle of the square a venue built on the parcel at `col`, `row` takes up."""
    first = parcel_centre(world_map, col, row)
    last_col, last_row = venue_footprint(world_map, col, row)[-1]
    last = parcel_centre(world_map, last_col, last_row)
    return Position((first.x + last.x) / 2, (first.y + last.y) / 2)


def facility_centre(world_map: WorldMap, col: int, row: int, kind: str | None) -> Position:
    """The middle of what the facility of `kind` on the parcel at `col`, `row` takes up; of the
    parcel itself when it is bare."""
    if kind in VENUE_FACILITIES:
        return venue_centre(world_map, col, row)
    return parcel_centre(world_map, col, row)


def claimed(world: World) -> dict[tuple[int, int], str]:
    """Which parcels are taken, by the id of the parcel whose facility or running building job
    takes them: its own, and for a venue the whole square it takes up."""
    taken: dict[tuple[int, int], str] = {}
    by_id = {parcel.id: parcel for parcel in world.parcels}
    for parcel in world.parcels:
        if parcel.facility is None:
            continue
        if parcel.facility.kind in VENUE_FACILITIES:
            cells = venue_footprint(world.map, parcel.col, parcel.row)
        else:
            cells = ((parcel.col, parcel.row),)
        taken.update(dict.fromkeys(cells, parcel.id))
    for job in world.compute.jobs:
        site = by_id.get(job.parcel_id or "")
        if site is None:
            continue
        if job.kind in ("build_game_hall", "build_workspace"):
            cells = venue_footprint(world.map, site.col, site.row)
        else:
            cells = ((site.col, site.row),)
        taken.update(dict.fromkeys(cells, site.id))
    return taken


def venue_room(world: World, parcel: Parcel, taken: dict[tuple[int, int], str]) -> bool:
    """Whether the owner of `parcel` could start a venue on it: it fits in the block and every
    parcel of the square is theirs and untaken."""
    if not venue_fits(world.map, parcel.col, parcel.row):
        return False
    mine = {(p.col, p.row) for p in world.parcels if p.owner_id == parcel.owner_id}
    return all(
        cell in mine and cell not in taken
        for cell in venue_footprint(world.map, parcel.col, parcel.row)
    )


def plot(world: World, col: int, row: int) -> Plot:
    """The grid square at `col`, `row`, which must lie on the map."""
    count = grid_size(world)
    if not 0 <= col < count or not 0 <= row < count:
        raise ValueError(f"no parcel at column {col}, row {row}")
    owner = next((p.owner_id for p in world.parcels if (p.col, p.row) == (col, row)), None)
    return replace(_layout(world.map)[row * count + col], owner_id=owner)


def parcel_centre(world_map: WorldMap, col: int, row: int) -> Position:
    """Centre of the grid square at `col`, `row` on `world_map`."""
    count = max(int(world_map.size // PARCEL_SIZE), 1)
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
    check_for_sale(world, square)
    world = take_coins(world, resident_id, square.price)
    world = record_transfer(world, resident_id, SYSTEM, square.price, "land")
    return add_block(world, square.col, square.row, resident_id)


def for_sale_at(world: World, position: Position) -> Plot | None:
    """The unowned, buyable land `position` stands on, or None in the lobby or on land that is
    owned."""
    square = plot_at(world, position)
    if square.lobby or square.owner_id is not None:
        return None
    return square


def check_for_sale(world: World, square: Plot) -> None:
    """Raise `LandError` unless the block of `square` can be bought: it is not the lobby and
    not already owned."""
    if square.lobby:
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
def _layout(world_map: WorldMap) -> tuple[Plot, ...]:
    """Every grid square without owners, row by row; worked out once per map, since every
    observation asks for it."""
    size = world_map.size
    town = town_centre(size)
    count = max(int(size // PARCEL_SIZE), 1)
    cells = world_map.block_cells
    blocks = count // cells
    if world_map.lobby and blocks % 2 == 0:
        raise ValueError("a lobby needs an odd number of blocks along each side")
    middle = blocks // 2

    def span(first: int, last: int) -> tuple[float, float]:
        """Where parcels `first` to `last` of a side lie; the last parcel reaches the edge."""
        return first * PARCEL_SIZE, size if last == count - 1 else (last + 1) * PARCEL_SIZE

    squares = []
    for row in range(count):
        for col in range(count):
            left, right = span(col, col)
            top, bottom = span(row, row)
            centre = Position((left + right) / 2, (top + bottom) / 2)
            first_col, first_row = col // cells * cells, row // cells * cells
            block_left, block_right = span(first_col, first_col + cells - 1)
            block_top, block_bottom = span(first_row, first_row + cells - 1)
            block_centre = Position((block_left + block_right) / 2, (block_top + block_bottom) / 2)
            price = _price(block_centre, town) * world_map.block_price_factor
            lobby = world_map.lobby and (col // cells, row // cells) == (middle, middle)
            squares.append(Plot(col, row, centre, price, None, lobby))
    return tuple(squares)


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
