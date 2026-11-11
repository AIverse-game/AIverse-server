"""Portals: facilities that join parcels into a transport network.

A portal is built on a parcel like an ad slot, by a generative job, see `compute.py`. It
earns only tolls: those of residents who teleport and a small fixed one from each visitor who
crosses a line that ends there. Two portals are joined by a line, a `Link`, with a toll. The
owners of both parcels must agree: `link_portals` joins two portals of one resident at once, while a
resident whose portal is to be joined to someone else's sends them a `LinkProposal`, an event,
see `events.py`, which they accept or reject. A portal may have at most `MAX_LINKS_PER_PORTAL`
lines, a resident may have at most `MAX_PORTALS` portals built or being built, and the two
parcels of a line lie at least `MIN_LINK_DISTANCE` apart, since a shorter line saves nothing.

Lines do two things.

- A resident standing in the parcel of a portal can `teleport` to the parcel at the other end
  of one of its lines, arriving at once instead of walking there, and pays the line's toll. The
  toll goes to the holders of the shares of both parcels, half to each end, see `shares.py`,
  ledger channel `toll`.
- `effective_distance` is how far a spot on the map is from the middle of it once the lines
  are counted: walk to a portal, take its lines for nothing, and walk on from the portal of
  the network that lies nearest the middle, or simply walk straight if that is shorter. Nothing
  uses it now that the visitors walk by `Router`; it is kept for the layout of the map.
- `Router` gives the same for a walk from one spot to another: the shorter of walking on foot
  (straight, or across the bridges between islands where the map has them, see `islands.py`)
  and walking to the nearest portal of a network, coming out at once at the portal of it
  nearest the goal. The scripted visitors, see `visitors.py`, walk by it, so which lines
  carry traffic is up to their choices. A trip through a network says which two portals it
  went in and out at, `Route.portals`, and `Router.path` the portals between them, by the
  lines with the fewest crossings; the visitor pays the system's small toll for each line,
  see `admissions.VISITOR_TOLL`, not the owners' tolls, which are for residents. The walk
  takes no account of the crossings, so the shape of a network changes what visitors pay and
  to whom, not which way they go.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from __future__ import annotations

import functools
import math
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from ..core.events import EventPayload
from ..core.inventory import add_coins, take_coins
from ..core.land import MAX_TOLL, Link, Parcel
from ..core.ledger import record_transfer
from ..core.world import Position, WorldMap
from .islands import walk_from
from .land import parcel_centre, plot, plot_at, town_centre
from .shares import MAX_PENDING_PER_PAIR, split_income

if TYPE_CHECKING:
    from ..core.events import Event
    from ..core.world import World

MAX_PORTALS = 4
"""Portals one resident may have, counting those still being built."""

MAX_LINKS_PER_PORTAL = 3
"""Lines one portal may end. With no limit one portal in the middle could pull the whole map
to the middle once visitors walk by effective distance."""

MIN_LINK_DISTANCE = 30.0
"""Least distance between the centres of the two parcels of a line, three parcels."""

PortalsErrorCode = Literal[
    "unknown_parcel",
    "no_portal",
    "not_your_portal",
    "same_portal",
    "already_linked",
    "too_many_links",
    "too_close",
    "invalid_toll",
    "not_at_portal",
    "not_linked",
    "too_many_offers",
]


class PortalsError(Exception):
    def __init__(self, code: PortalsErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: PortalsErrorCode = code
        self.message = message


def portal_count(world: World, resident_id: str) -> int:
    """Portals the resident has built or is building."""
    built = sum(
        parcel.owner_id == resident_id
        and parcel.facility is not None
        and parcel.facility.kind == "portal"
        for parcel in world.parcels
    )
    building = sum(
        job.owner_id == resident_id and job.kind == "build_portal" for job in world.compute.jobs
    )
    return built + building


def links_of(world: World, parcel_id: str) -> tuple[Link, ...]:
    """The lines that end at `parcel_id`."""
    return tuple(link for link in world.links if link.joins(parcel_id))


def link_portals(
    world: World, resident_id: str, parcel_id: str, other_parcel_id: str, toll: int
) -> World:
    """Join two portals that both belong to the resident with a line costing `toll` per trip."""
    mine, other = _check_link(world, resident_id, parcel_id, other_parcel_id, toll)
    if other.owner_id != resident_id:
        raise PortalsError(
            "not_your_portal", f"parcel {other_parcel_id!r} is not {resident_id}'s: propose it"
        )
    return _with_link(world, mine, other, toll)


@dataclass(frozen=True)
class LinkProposal(EventPayload):
    """The sender asks the recipient to join the sender's portal on `parcel_id` to the
    recipient's on `other_parcel_id` with a line costing `toll` per trip."""

    parcel_id: str
    other_parcel_id: str
    toll: int

    def on_send(self, world: World, event: Event) -> World:
        _, other = _check_link(
            world, event.sender_id, self.parcel_id, self.other_parcel_id, self.toll
        )
        if other.owner_id != event.recipient_id:
            raise PortalsError("not_your_portal", f"parcel {self.other_parcel_id!r} is not theirs")
        waiting = sum(
            other_event.sender_id == event.sender_id
            and other_event.recipient_id == event.recipient_id
            and isinstance(other_event.payload, LinkProposal)
            for other_event in world.events.pending
        )
        if waiting >= MAX_PENDING_PER_PAIR:
            raise PortalsError(
                "too_many_offers",
                f"you already have {MAX_PENDING_PER_PAIR} line proposals waiting for them",
            )
        return world

    def on_accept(self, world: World, event: Event) -> World:
        mine, other = _check_link(
            world, event.sender_id, self.parcel_id, self.other_parcel_id, self.toll
        )
        if other.owner_id != event.recipient_id:
            raise PortalsError("not_your_portal", f"parcel {self.other_parcel_id!r} is not yours")
        return _with_link(world, mine, other, self.toll)


def teleport(world: World, resident_id: str, to_parcel_id: str) -> World:
    """Move the resident, who stands in the parcel of a portal, to the parcel at the other end
    of one of its lines at once. They pay the line's toll to the holders of the shares of both
    parcels, half each, the departure end taking the odd coin. The caller checks they may act
    and are not busy, see `actions.perform`."""
    resident = next(r for r in world.residents if r.id == resident_id)
    here = plot_at(world, resident.position)
    start = _parcel(world, here.id)
    if start is None or start.facility is None or start.facility.kind != "portal":
        raise PortalsError("not_at_portal", f"parcel {here.id} has no portal to travel from")
    link = next(
        (
            line
            for line in world.links
            if line.joins(here.id) and line.other(here.id) == to_parcel_id
        ),
        None,
    )
    if link is None:
        raise PortalsError("not_linked", f"the portal on {here.id} has no line to {to_parcel_id}")
    end = _parcel(world, to_parcel_id)
    assert end is not None  # Every link joins parcels that exist, see `World`.
    if link.toll:
        world = take_coins(world, resident_id, link.toll)
    arrival_part = link.toll // 2
    for parcel, part in ((start, link.toll - arrival_part), (end, arrival_part)):
        if not part:
            continue
        for holder, coins in split_income(parcel, part):
            world = add_coins(world, holder, coins)
            # Paying oneself changes nothing and leaves nothing to record.
            if holder != resident_id:
                world = record_transfer(world, resident_id, holder, coins, "toll")
    centre = plot(world, end.col, end.row).centre
    residents = tuple(
        replace(r, position=centre) if r.id == resident_id else r for r in world.residents
    )
    return replace(world, residents=residents)


def effective_distance(world: World, centre: Position) -> float:
    """How far `centre` is from the middle of the map once lines are counted, see the module
    docstring. Never more than the straight distance."""
    town = town_centre(world.map.size)
    best = math.dist((centre.x, centre.y), (town.x, town.y))
    if not world.links:
        return best
    portals = tuple(
        sorted(
            (parcel.id, parcel.col, parcel.row)
            for parcel in world.parcels
            if parcel.facility is not None and parcel.facility.kind == "portal"
        )
    )
    lines = tuple(sorted((link.a, link.b) for link in world.links))
    for there, from_there in _network(world.map, portals, lines):
        best = min(best, math.dist((centre.x, centre.y), (there.x, there.y)) + from_there)
    return best


@functools.lru_cache(maxsize=32)
def _components(
    world_map: WorldMap,
    portals: tuple[tuple[str, int, int], ...],
    lines: tuple[tuple[str, str], ...],
) -> tuple[tuple[Position, str, str], ...]:
    """Each portal's centre, with a name for its network (the portals its lines reach, and
    theirs in turn), the same for all portals of one network, and the id of its parcel. Worked
    out once per layout of portals and lines, since a layout is asked about many times."""
    centres = {parcel_id: parcel_centre(world_map, col, row) for parcel_id, col, row in portals}
    group = {parcel_id: parcel_id for parcel_id in centres}

    def root(parcel_id: str) -> str:
        while group[parcel_id] != parcel_id:
            group[parcel_id] = group[group[parcel_id]]
            parcel_id = group[parcel_id]
        return parcel_id

    for first, second in lines:
        group[root(first)] = root(second)
    return tuple((there, root(parcel_id), parcel_id) for parcel_id, there in centres.items())


def _layout(world: World) -> tuple[tuple[Position, str, str], ...]:
    portals = tuple(
        sorted(
            (parcel.id, parcel.col, parcel.row)
            for parcel in world.parcels
            if parcel.facility is not None and parcel.facility.kind == "portal"
        )
    )
    lines = tuple(sorted((link.a, link.b) for link in world.links))
    return _components(world.map, portals, lines)


@functools.lru_cache(maxsize=32)
def _network(
    world_map: WorldMap,
    portals: tuple[tuple[str, int, int], ...],
    lines: tuple[tuple[str, str], ...],
) -> tuple[tuple[Position, float], ...]:
    """Each portal's centre, with the distance from the middle of the map of the portal in its
    network that lies nearest to the middle."""
    town = town_centre(world_map.size)
    components = _components(world_map, portals, lines)
    nearest: dict[str, float] = {}
    for there, name, _ in components:
        gap = math.dist((there.x, there.y), (town.x, town.y))
        nearest[name] = min(nearest.get(name, gap), gap)
    return tuple((there, nearest[name]) for there, name, _ in components)


@dataclass(frozen=True)
class Route:
    """The way a visitor goes from one spot to another."""

    distance: float
    """Map units walked; the trip through a network is not counted."""
    legs: tuple[tuple[Position, Position], ...]
    """The straight stretches walked, in order; going through a network of portals leaves a break
    between the one that ends at the portal walked to and the one that starts at the portal come
    out of."""
    portals: tuple[str, str] | None = None
    """The parcels of the portal it went in at and the one it came out of, None when it only
    walked. Never the same one twice: going in and out at one portal is no trip through a
    network."""


class Router:
    """Shortest ways on foot and through the networks of portals from one spot, `start`, to
    many: it works out once which portal of each network is nearest to `start`. A walker goes
    through at most one network, since a second one would rarely save anything and the portals
    of one network are joined by lines already. On foot a walker keeps to the islands and
    bridges where the map has them, see `islands.py`."""

    def __init__(self, world: World, start: Position) -> None:
        self._map = world.map
        self._links = world.links
        self._start = walk_from(world.map, start)
        self._portals = _layout(world)
        self._entry: dict[str, tuple[float, Position, str]] = {}
        for there, name, parcel_id in self._portals:
            gap = self._start.distance(there)
            if name not in self._entry or gap < self._entry[name][0]:
                self._entry[name] = (gap, there, parcel_id)

    def path(self, route: Route) -> tuple[str, ...]:
        """The parcels of the portals `route` goes through, in order, from the one it goes in at
        to the one it comes out of, by the lines with the fewest crossings, the lower parcel id
        first among equals; empty when it only walks. The visitor crosses each line between two
        of them, taking no time."""
        if route.portals is None:
            return ()
        return _line_path(self._links, *route.portals)

    def to(self, end: Position) -> Route:
        """The shortest way to `end`. The tolls are not counted in the distance: a visitor does
        not weigh coins."""
        best = self._start.distance(end)
        via: tuple[Position, str, Position, str] | None = None
        for there, name, parcel_id in self._portals:
            gap, entry, entry_id = self._entry[name]
            total = gap + walk_from(self._map, there).distance(end)
            if total < best:
                best, via = total, (entry, entry_id, there, parcel_id)
        if via is None:
            return Route(best, self._start.legs(end))
        entry, entry_id, there, exit_id = via
        legs = (*self._start.legs(entry), *walk_from(self._map, there).legs(end))
        return Route(best, legs, (entry_id, exit_id) if entry_id != exit_id else None)


def _line_path(links: tuple[Link, ...], start: str, end: str) -> tuple[str, ...]:
    """The parcels from `start` to `end` along lines, with as few lines as possible."""
    came_from: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        for there in sorted(link.other(here) for link in links if link.joins(here)):
            if there in came_from:
                continue
            came_from[there] = here
            queue.append(there)
    if end not in came_from:
        raise AssertionError(f"{start} and {end} are in one network, joined by lines")
    path = [end]
    while (before := came_from[path[-1]]) is not None:
        path.append(before)
    return tuple(reversed(path))


def _check_link(
    world: World, proposer_id: str, parcel_id: str, other_parcel_id: str, toll: int
) -> tuple[Parcel, Parcel]:
    """Refuse a line the proposer may not draw from their portal on `parcel_id` to the one on
    `other_parcel_id`, or return the two parcels."""
    if type(toll) is not int or not 0 <= toll <= MAX_TOLL:
        raise PortalsError("invalid_toll", f"the toll is an integer in 0..{MAX_TOLL} coins")
    mine = _portal(world, parcel_id)
    other = _portal(world, other_parcel_id)
    if mine.owner_id != proposer_id:
        raise PortalsError("not_your_portal", f"parcel {parcel_id!r} is not {proposer_id}'s")
    if mine.id == other.id:
        raise PortalsError("same_portal", "a line joins two different portals")
    if any(link.joins(mine.id) and link.joins(other.id) for link in world.links):
        raise PortalsError("already_linked", f"{mine.id} and {other.id} are already linked")
    for parcel in (mine, other):
        if len(links_of(world, parcel.id)) >= MAX_LINKS_PER_PORTAL:
            raise PortalsError(
                "too_many_links", f"the portal on {parcel.id} already has {MAX_LINKS_PER_PORTAL}"
            )
    a, b = plot(world, mine.col, mine.row).centre, plot(world, other.col, other.row).centre
    if math.dist((a.x, a.y), (b.x, b.y)) < MIN_LINK_DISTANCE:
        raise PortalsError(
            "too_close", f"the portals must be at least {MIN_LINK_DISTANCE:g} units apart"
        )
    return mine, other


def _portal(world: World, parcel_id: str) -> Parcel:
    parcel = _parcel(world, parcel_id)
    if parcel is None:
        raise PortalsError("unknown_parcel", f"no one owns a parcel {parcel_id!r}")
    if parcel.facility is None or parcel.facility.kind != "portal":
        raise PortalsError("no_portal", f"parcel {parcel_id} has no portal")
    return parcel


def _parcel(world: World, parcel_id: str) -> Parcel | None:
    return next((p for p in world.parcels if p.id == parcel_id), None)


def _with_link(world: World, mine: Parcel, other: Parcel, toll: int) -> World:
    return replace(world, links=(*world.links, Link(mine.id, other.id, toll)))
