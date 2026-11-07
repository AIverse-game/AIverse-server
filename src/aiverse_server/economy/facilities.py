"""Facilities: what residents build on their land to earn coins from visitors.

Visitors are not simulated one by one. Every `FACILITY_SECONDS` after it was built, a facility
pays its owner coins for the visitors who passed by, see `ad_income`. Visitors come from
outside the town, so this money comes from the system, like the market's. Their number grows
as the game goes on, `visitors`, and they crowd towards the market at the centre, `footfall`;
residents standing nearby add to the crowd. Ad slots near one another share the same crowd.

Facilities are built on a bare parcel the resident owns and stands in, of carried materials,
see `studio.py`:

- An ad slot, `AD_MATERIALS`, earns by the footfall where it stands.
- A server, `SERVER_MATERIALS`, earns nothing from visitors. Anyone standing in its parcel can
  supply it compute, up to `SERVER_CAPACITY`; it spends `MEETING_COMPUTE` of it hosting a
  meeting room within `HOST_RADIUS` for a cycle, and its owner charges other owners' rooms the
  fee they set.
- A meeting room, `MEETING_MATERIALS` and a `MEETING_PROGRAM` program, earns by the visitors
  wherever it stands, shared with the meeting rooms around it, but only in a cycle a server
  hosts it, see `host_for`; the hosting fee comes out of that cycle's income.

A facility whose owner has left the game earns nothing more, nor hosts, and none earns once the
game is over; nothing is paid back for them.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from collections.abc import Iterable
from dataclasses import replace
from typing import Literal

from ..core.inventory import add_coins, item_count, take_coins, take_item
from ..core.land import Facility, Parcel
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import Position, Resident, World
from .compute import COMPUTE
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

SERVER_MATERIALS = 10
"""Materials a server uses up."""

SERVER_CAPACITY = 300
"""Most compute a server holds."""

HOST_RADIUS = 30.0
"""A server hosts facilities whose parcel centres lie this close to its own."""

MEETING_MATERIALS = 6
"""Materials a meeting room uses up."""

MEETING_PROGRAM = "v1"
"""Tier of the program a meeting room uses up."""

MEETING_COMPUTE = 4
"""Compute a server spends hosting a meeting room for a cycle."""

MEETING_RATE = 2.0
"""Coins a meeting room earns per cycle for every visitor at the market, before sharing."""

MEETING_SHARE_RADIUS = 40.0
"""Meeting rooms whose parcel centres lie this close share their visitors."""

FacilityErrorCode = Literal[
    "unknown_resident",
    "not_your_parcel",
    "parcel_built",
    "not_enough_materials",
    "not_enough_programs",
    "invalid_fee",
    "invalid_amount",
    "no_server",
    "server_closed",
    "server_full",
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
    working = _centres(world, _working(world, "ad"))
    return {
        (square.col, square.row): int(
            AD_RATE
            * footfall(world, square.centre)
            / (_sharing(working, square, AD_SHARE_RADIUS) + 1)
        )
        for square in squares
    }


def meeting_income(world: World, col: int, row: int) -> int:
    """Coins a meeting room on the parcel at `col`, `row` earns in a cycle a server hosts it,
    before the hosting fee: the visitors split evenly with the working meeting rooms around it.
    For a parcel without one, what one built there now would earn."""
    return meeting_incomes(world, (plot(world, col, row),))[(col, row)]


def meeting_incomes(world: World, squares: Iterable[Plot]) -> dict[tuple[int, int], int]:
    """`meeting_income` for many parcels at once, by column and row. A meeting room counts
    whether or not a server hosts it; one whose owner has left does not."""
    working = _centres(world, _working(world, "meeting"))
    return {
        (square.col, square.row): int(
            MEETING_RATE
            * visitors(world.time)
            / (_sharing(working, square, MEETING_SHARE_RADIUS) + 1)
        )
        for square in squares
    }


def host_for(world: World, centre: Position, owner_id: str) -> Parcel | None:
    """The server that would host a meeting room of `owner_id` centred at `centre` now: of the
    working servers within `HOST_RADIUS` holding `MEETING_COMPUTE`, the owner's own first,
    then the cheapest for them, then the nearest. None when there is none."""
    candidates = [
        (parcel, _distance(plot(world, parcel.col, parcel.row).centre, centre))
        for parcel in _working(world, "server")
        if parcel.facility is not None and parcel.facility.compute >= MEETING_COMPUTE
    ]
    in_reach = [(parcel, distance) for parcel, distance in candidates if distance <= HOST_RADIUS]
    if not in_reach:
        return None
    return min(
        in_reach,
        key=lambda pair: (
            pair[0].owner_id != owner_id,
            hosting_fee(pair[0], owner_id),
            pair[1],
            pair[0].row,
            pair[0].col,
        ),
    )[0]


def hosting_fee(server: Parcel, owner_id: str) -> int:
    """Coins `server` charges a facility of `owner_id` per cycle: nothing for its own owner."""
    return 0 if server.owner_id == owner_id else _fee(server)


def build_ad(world: World, resident_id: str) -> World:
    """Build an ad slot on the parcel the resident stands in, which they must own; the caller
    checks they may act and are not busy, see `actions.perform`."""
    parcel = _bare_parcel(world, resident_id)
    world = _use_materials(world, resident_id, AD_MATERIALS, "an ad slot")
    ad = Facility("ad", world.time, world.time + FACILITY_SECONDS)
    return _with_facility(world, parcel, ad)


def build_server(world: World, resident_id: str, fee: int) -> World:
    """Build a server charging `fee` on the parcel the resident stands in, which they must own;
    it starts without compute. The caller checks they may act and are not busy."""
    _check_fee(fee)
    parcel = _bare_parcel(world, resident_id)
    world = _use_materials(world, resident_id, SERVER_MATERIALS, "a server")
    return _with_facility(world, parcel, Facility("server", world.time, None, fee=fee))


def build_meeting_room(world: World, resident_id: str) -> World:
    """Build a meeting room on the parcel the resident stands in, which they must own; the
    caller checks they may act and are not busy."""
    parcel = _bare_parcel(world, resident_id)
    held = item_count(world, resident_id, MEETING_PROGRAM)
    if held < 1:
        raise FacilityError(
            "not_enough_programs",
            f"a meeting room takes a {MEETING_PROGRAM} program, resident {resident_id!r} has none",
        )
    world = _use_materials(world, resident_id, MEETING_MATERIALS, "a meeting room")
    world = take_item(world, resident_id, MEETING_PROGRAM)
    room = Facility("meeting", world.time, world.time + FACILITY_SECONDS)
    return _with_facility(world, parcel, room)


def supply_server(world: World, resident_id: str, amount: int) -> World:
    """Put `amount` carried compute into the server on the parcel the resident stands in,
    whoever owns it. The caller checks they may act and are not busy."""
    if type(amount) is not int or amount < 1:
        raise FacilityError("invalid_amount", "supply a positive whole amount of compute")
    resident = _resident(world, resident_id)
    square = plot_at(world, resident.position)
    parcel = _parcel(world, square.col, square.row)
    if parcel is None or parcel.facility is None or parcel.facility.kind != "server":
        raise FacilityError("no_server", f"there is no server on parcel {square.id}")
    server = parcel.facility
    if _owner(world, parcel).left:
        raise FacilityError("server_closed", f"the owner of server {square.id} left the game")
    room = SERVER_CAPACITY - server.compute
    if amount > room:
        raise FacilityError(
            "server_full", f"server {square.id} holds {server.compute}, room for {room} more"
        )
    world = take_item(world, resident_id, COMPUTE, amount)
    return _with_facility(world, parcel, replace(server, compute=server.compute + amount))


def set_hosting_fee(world: World, resident_id: str, server_id: str, fee: int) -> World:
    """Change what the resident's server `server_id` charges, from anywhere."""
    _check_fee(fee)
    parcel = next((p for p in world.parcels if p.id == server_id), None)
    if (
        parcel is None
        or parcel.owner_id != resident_id
        or parcel.facility is None
        or parcel.facility.kind != "server"
    ):
        raise FacilityError("no_server", f"{server_id!r} is not a server of {resident_id!r}")
    return _with_facility(world, parcel, replace(parcel.facility, fee=fee))


def finish_facilities(world: World) -> World:
    """Pay out every facility whose cycle is due by `world.time`. The game clock calls in
    chronological order, not in bulk."""
    if world.ended:
        return world
    due = [
        p
        for p in world.parcels
        if p.facility is not None
        and p.facility.next_at is not None
        and p.facility.next_at <= world.time
    ]
    # Incomes are worked out before any is paid, so the order of the parcels does not change
    # them; meeting rooms due at once draw on servers in the order their parcels were bought.
    incomes = {(p.col, p.row): _income(world, p) for p in due}
    for original in due:
        parcel = _parcel(world, original.col, original.row)
        assert parcel is not None and parcel.facility is not None
        facility = parcel.facility
        income = incomes[(parcel.col, parcel.row)]
        host = None
        if facility.kind == "meeting" and income:
            host = host_for(world, plot(world, parcel.col, parcel.row).centre, parcel.owner_id)
            if host is None:
                income = 0
        if income:
            world = add_coins(world, parcel.owner_id, income)
            world = record_transfer(world, SYSTEM, parcel.owner_id, income, "visitors")
        fee = 0
        if host is not None:
            fee = min(hosting_fee(host, parcel.owner_id), income)
            world = _host(world, host, parcel.owner_id, fee)
        assert facility.next_at is not None
        paid = replace(
            facility,
            next_at=facility.next_at + FACILITY_SECONDS,
            earned=facility.earned + income - fee,
            last_income=income - fee,
            host=None if host is None else host.id,
        )
        world = _with_facility(world, parcel, paid)
    return world


def next_facility_cycle(world: World) -> float | None:
    """Game time when the earliest facility pays out next, or None when there is none or the
    game is over."""
    if world.ended:
        return None
    return min(
        (
            p.facility.next_at
            for p in world.parcels
            if p.facility is not None and p.facility.next_at is not None
        ),
        default=None,
    )


def _income(world: World, parcel: Parcel) -> int:
    """What a facility earns this cycle before any hosting, 0 once its owner has left."""
    assert parcel.facility is not None
    if _owner(world, parcel).left:
        return 0
    if parcel.facility.kind == "meeting":
        return meeting_income(world, parcel.col, parcel.row)
    return ad_income(world, parcel.col, parcel.row)


def _host(world: World, server: Parcel, owner_id: str, fee: int) -> World:
    """Have `server` host a facility of `owner_id` for a cycle, paid `fee` out of its income."""
    assert server.facility is not None
    if fee:
        world = take_coins(world, owner_id, fee)
        world = add_coins(world, server.owner_id, fee)
        world = record_transfer(world, owner_id, server.owner_id, fee, "hosting")
    hosted = replace(
        server.facility,
        compute=server.facility.compute - MEETING_COMPUTE,
        earned=server.facility.earned + fee,
    )
    return _with_facility(world, server, hosted)


def _bare_parcel(world: World, resident_id: str) -> Parcel:
    """The parcel the resident stands in, which they must own and nothing stands on."""
    square = plot_at(world, _resident(world, resident_id).position)
    parcel = _parcel(world, square.col, square.row)
    if parcel is None or parcel.owner_id != resident_id:
        raise FacilityError("not_your_parcel", f"parcel {square.id} is not {resident_id}'s")
    if parcel.facility is not None:
        raise FacilityError("parcel_built", f"parcel {square.id} already has a facility")
    return parcel


def _use_materials(world: World, resident_id: str, needed: int, what: str) -> World:
    held = item_count(world, resident_id, MATERIAL)
    if held < needed:
        raise FacilityError(
            "not_enough_materials",
            f"{what} takes {needed} materials, resident {resident_id!r} carries {held}",
        )
    return take_item(world, resident_id, MATERIAL, needed)


def _check_fee(fee: int) -> None:
    if type(fee) is not int or fee < 0:
        raise FacilityError("invalid_fee", "a hosting fee is a whole number of coins >= 0")


def _fee(server: Parcel) -> int:
    assert server.facility is not None
    return server.facility.fee


def _working(world: World, kind: str) -> list[Parcel]:
    """Parcels with a facility of `kind` whose owner is still in the game."""
    gone = {resident.id for resident in world.residents if resident.left}
    return [
        p
        for p in world.parcels
        if p.facility is not None and p.facility.kind == kind and p.owner_id not in gone
    ]


def _centres(world: World, parcels: list[Parcel]) -> list[tuple[tuple[int, int], Position]]:
    return [((p.col, p.row), plot(world, p.col, p.row).centre) for p in parcels]


def _sharing(working: list[tuple[tuple[int, int], Position]], square: Plot, radius: float) -> int:
    """How many of the `working` parcels, by column and row with their centres, other than
    `square` lie within `radius` of it."""
    return sum(
        1
        for spot, centre in working
        if spot != (square.col, square.row) and _distance(centre, square.centre) <= radius
    )


def _resident(world: World, resident_id: str) -> Resident:
    resident = next((r for r in world.residents if r.id == resident_id), None)
    if resident is None:
        raise FacilityError("unknown_resident", f"no resident with id {resident_id!r}")
    return resident


def _owner(world: World, parcel: Parcel) -> Resident:
    return next(r for r in world.residents if r.id == parcel.owner_id)


def _parcel(world: World, col: int, row: int) -> Parcel | None:
    return next((p for p in world.parcels if (p.col, p.row) == (col, row)), None)


def _with_facility(world: World, parcel: Parcel, facility: Facility) -> World:
    parcels = tuple(
        replace(p, facility=facility) if (p.col, p.row) == (parcel.col, parcel.row) else p
        for p in world.parcels
    )
    return replace(world, parcels=parcels)


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))
