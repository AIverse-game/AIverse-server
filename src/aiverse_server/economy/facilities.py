"""Facilities: what residents build on their land to earn coins from visitors.

Visitors are not simulated one by one. Every `FACILITY_SECONDS` after it was built, a facility
pays its owner coins for the visitors who passed by, see `ad_income`. Visitors come from
outside the town, so this money comes from the system, like the market's. Their number grows
as the game goes on, `visitors`, and they crowd towards the market at the centre, `footfall`;
residents standing nearby add to the crowd. Ad slots near one another share the same crowd.

So far the only facility is the ad slot: on a parcel the resident owns and stands in, it uses
up `AD_MATERIALS` carried materials, see `studio.py`. A facility whose owner has left the game
earns nothing more, and none earns once the game is over; nothing is paid back for them.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from collections.abc import Iterable
from dataclasses import replace
from typing import Literal

from ..core.inventory import add_coins, item_count, take_item
from ..core.land import Facility, Parcel
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import Position, World
from .land import Plot, market_centre, plot, plot_at
from .studio import MATERIAL

FACILITY_SECONDS = 30.0
"""Game seconds between two payouts of a facility."""

AD_MATERIALS = 8
"""Materials an ad slot uses up."""

AD_RATE = 2.0
"""Coins an ad slot earns per cycle for every unit of footfall it does not share."""

AD_SHARE_RADIUS = 20.0
"""Ad slots whose parcel centres lie this close share the footfall between them."""

VISITORS_AT_START = 100.0
"""Footfall at the market when the game starts."""

VISITOR_GROWTH_SECONDS = 1_200.0
"""Game seconds over which the visitors grow by another `VISITORS_AT_START`."""

FOOTFALL_REACH = 75.0
"""Distance from the market at which the share of visitors passing has fallen, in a straight
line, from all of them to `FOOTFALL_FLOOR`; it stays there farther out."""

FOOTFALL_FLOOR = 0.25
"""Share of the visitors that pass even the farthest parcel."""

RESIDENT_FOOTFALL = 10.0
"""Footfall each resident standing within `RESIDENT_REACH` of a parcel's centre adds."""

RESIDENT_REACH = 15.0

FacilityErrorCode = Literal[
    "unknown_resident", "not_your_parcel", "parcel_built", "not_enough_materials"
]


class FacilityError(Exception):
    def __init__(self, code: FacilityErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: FacilityErrorCode = code
        self.message = message


def visitors(time: float) -> float:
    """Visitors at the market at game time `time`; they grow in a straight line."""
    return VISITORS_AT_START * (1 + time / VISITOR_GROWTH_SECONDS)


def footfall(world: World, centre: Position) -> float:
    """Visitors and residents passing a parcel centred at `centre` right now."""
    distance = _distance(centre, market_centre(world))
    share = 1 - (1 - FOOTFALL_FLOOR) * min(distance / FOOTFALL_REACH, 1.0)
    standing = sum(
        1
        for resident in world.residents
        if not resident.left
        and resident.walking is None
        and _distance(resident.position, centre) <= RESIDENT_REACH
    )
    return visitors(world.time) * share + RESIDENT_FOOTFALL * standing


def ad_income(world: World, col: int, row: int) -> int:
    """Coins an ad slot on the parcel at `col`, `row` earns per cycle right now: its footfall
    split evenly with the working ad slots around it. For a parcel without one, what one built
    there now would earn."""
    return ad_incomes(world, (plot(world, col, row),))[(col, row)]


def ad_incomes(world: World, squares: Iterable[Plot]) -> dict[tuple[int, int], int]:
    """`ad_income` for many parcels at once, by column and row. An ad slot whose owner has left
    takes no share of the footfall: it is no longer working."""
    working = [
        ((parcel.col, parcel.row), plot(world, parcel.col, parcel.row).centre)
        for parcel in _working(world)
    ]
    incomes = {}
    for square in squares:
        spot = (square.col, square.row)
        sharing = sum(
            1
            for other, centre in working
            if other != spot and _distance(centre, square.centre) <= AD_SHARE_RADIUS
        )
        incomes[spot] = int(AD_RATE * footfall(world, square.centre) / (sharing + 1))
    return incomes


def build_ad(world: World, resident_id: str) -> World:
    """Build an ad slot on the parcel the resident stands in, which they must own; the caller
    checks they may act and are not busy, see `actions.perform`."""
    resident = next((r for r in world.residents if r.id == resident_id), None)
    if resident is None:
        raise FacilityError("unknown_resident", f"no resident with id {resident_id!r}")
    square = plot_at(world, resident.position)
    parcel = next((p for p in world.parcels if (p.col, p.row) == (square.col, square.row)), None)
    if parcel is None or parcel.owner_id != resident_id:
        raise FacilityError("not_your_parcel", f"parcel {square.id} is not {resident_id}'s")
    if parcel.facility is not None:
        raise FacilityError("parcel_built", f"parcel {square.id} already has a facility")
    held = item_count(world, resident_id, MATERIAL)
    if held < AD_MATERIALS:
        raise FacilityError(
            "not_enough_materials",
            f"an ad slot takes {AD_MATERIALS} materials, resident {resident_id!r} carries {held}",
        )
    world = take_item(world, resident_id, MATERIAL, AD_MATERIALS)
    ad = Facility("ad", world.time, world.time + FACILITY_SECONDS)
    return _with_facility(world, parcel, ad)


def finish_facilities(world: World) -> World:
    """Pay out every facility whose cycle is due by `world.time`. The game clock calls in
    chronological order, not in bulk."""
    if world.ended:
        return world
    due = [p for p in world.parcels if p.facility is not None and p.facility.next_at <= world.time]
    # Incomes are worked out before any is paid, so the order of the parcels does not matter.
    incomes = {(p.col, p.row): _income(world, p) for p in due}
    for parcel in due:
        facility = parcel.facility
        assert facility is not None
        income = incomes[(parcel.col, parcel.row)]
        if income:
            world = add_coins(world, parcel.owner_id, income)
            world = record_transfer(world, SYSTEM, parcel.owner_id, income, "visitors")
        paid = replace(
            facility,
            next_at=facility.next_at + FACILITY_SECONDS,
            earned=facility.earned + income,
            last_income=income,
        )
        world = _with_facility(world, parcel, paid)
    return world


def next_facility_cycle(world: World) -> float | None:
    """Game time when the earliest facility pays out next, or None when there is none or the
    game is over."""
    if world.ended:
        return None
    return min((p.facility.next_at for p in world.parcels if p.facility is not None), default=None)


def _income(world: World, parcel: Parcel) -> int:
    owner = next(r for r in world.residents if r.id == parcel.owner_id)
    if owner.left:
        return 0
    return ad_income(world, parcel.col, parcel.row)


def _working(world: World) -> list[Parcel]:
    """Parcels with an ad slot whose owner is still in the game."""
    gone = {resident.id for resident in world.residents if resident.left}
    return [p for p in world.parcels if p.facility is not None and p.owner_id not in gone]


def _with_facility(world: World, parcel: Parcel, facility: Facility) -> World:
    parcels = tuple(
        replace(p, facility=facility) if (p.col, p.row) == (parcel.col, parcel.row) else p
        for p in world.parcels
    )
    return replace(world, parcels=parcels)


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
