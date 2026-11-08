"""Visitors: scripted tourists who walk the map, so that where the traffic goes is up to the
layout of the town and not fixed beforehand.

There are `VISITOR_COUNT` of them, each standing for more and more people as the game goes on,
`visitor_weight`. A visitor comes in through the entrance, the middle of the map, and makes a
few stops at ad slots showing content. On `ROUTE_SHARE` of the trips the visitor is a working
tourist: it differs from the others in nothing but a rough route, to a workspace and then to a game
hall showing games, instead of the ad slots, paying the system-set fee at each door, see
`admissions.py`. A stop with nowhere to go on the route is skipped, and with none left the visitor
leaves. Then the visitor walks back out and stays away a while before coming again. Nothing
about it is stored beyond where it set off from and to, and when: where it is in between is
worked out from that, `visitor_position`, and something happens only when a walk or a stay
ends, `advance_crowd`, at the times `next_visit` gives.

Where to next, `_head_for`: among the facilities of the kind it needs (for the others the ad
slots showing content that it has not been counted at lately), it picks by a multinomial logit,
so every facility has a chance but better ones more. The utility is `APPEAL` times the log of one
plus the pieces of content shown (a workspace counts as `WORKSPACE_PIECES`), less
`WALK_COST` per second of walking and `CROWD_COST` per `CROWD_CAPACITY` visitors already going
or standing there, plus noise. The walk is the shorter of walking on foot and going through a
network of portals, see `portals.Router`, so the lines change which slots are near, and whether
a visitor takes one is its own choice. Visitors use the lines for nothing. Where the map has
islands the walk keeps to them and crosses from one to the next only by the bridges, see
`islands.py`; residents are not held to that. With nothing worth seeing the visitor roams to a
random spot nearby, on its own island, and looks again.

Choices are random but the dice are not stateful: each is a hash of the crowd's seed, the
visitor, its trip, its step and what is being chosen, so the same game plays out the same way.

A visitor, on whatever route, counts as having seen a slot when it spends at least
`MIN_VIEW_SECONDS` within `VIEW_RADIUS` of it, standing there or passing by on the way to
somewhere else, and is not counted at the same slot again for
`SEEN_FOR` game seconds, even on a later trip. The count is made when the walk ends, judging
by the slots showing content then, and adds the people the visitor
stands for to the views of the slot's parcel, `Crowd.views`, which the sponsors pay for, see
`sponsors.py`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched.
"""

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace

from ..core.visitors import Crowd, Goal, Visitor
from ..core.world import Position, World
from .admissions import FEES, admit
from .ads import shown
from .islands import on_island
from .land import parcel_centre, town_centre
from .portals import Router

VISITOR_COUNT = 200
"""Visitors in the crowd."""

VISITOR_SPEED = 2.5
"""Map units a visitor walks per game second: the same as a resident, see
`residents/walking.py`, which this package does not import."""

VISITOR_GROWTH_SECONDS = 1_200.0
"""Game seconds over which the people each visitor stands for grow by another one."""

FIRST_ENTRY_SPREAD = 120.0
"""Game seconds over which the crowd first comes in, so they do not all arrive together."""

MIN_STOPS = 2
MAX_STOPS = 4
"""Ad slots a visitor stops at on one trip, at least and at most."""

MIN_DWELL = 6.0
MAX_DWELL = 14.0
"""Game seconds a visitor stays at an ad slot it has come to see, least and most."""

DWELL: dict[str, tuple[float, float]] = {
    "ad": (MIN_DWELL, MAX_DWELL),
    "workspace": (20.0, 40.0),
    "game_hall": (10.0, 20.0),
}
"""Game seconds a visitor stays at a facility of each kind, least and most."""

ROUTE_SHARE = 0.3
"""Share of trips that follow the working route, to a workspace and then a game hall, instead
of looking at ad slots."""

WORKSPACE_PIECES = 1
"""A workspace shows no content, so it draws visitors as an ad slot showing this many pieces
would."""

MIN_AWAY = 20.0
MAX_AWAY = 60.0
"""Game seconds a visitor stays away between trips, least and most."""

MIN_ROAM = 10.0
MAX_ROAM = 30.0
"""Map units a visitor roams when nothing is worth seeing, least and most."""

VIEW_RADIUS = 10.0
"""A visitor within this distance of a slot's parcel centre can see it."""

MIN_VIEW_SECONDS = 4.0
"""Game seconds within `VIEW_RADIUS` of a slot that make a view; passing straight through its
whole circle takes 8."""

SEEN_FOR = 120.0
"""Game seconds after being counted at a slot during which a visitor is not counted there again,
and does not choose it."""

APPEAL = 2.0
"""Utility for each unit of the log of one plus the pieces of content a slot shows."""

WALK_COST = 0.05
"""Utility lost per game second of walking to a slot."""

CROWD_COST = 2.0
"""Utility lost when as many visitors as `CROWD_CAPACITY` are already going to or standing at a
slot."""

CROWD_CAPACITY = 8
"""Visitors a slot can take before it crowds; more make it less attractive, in proportion."""


def new_crowd(count: int = VISITOR_COUNT, seed: int = 0) -> Crowd:
    """A crowd of `count` visitors, none in town yet: each comes in at some point in the first
    `FIRST_ENTRY_SPREAD` game seconds."""
    visitors = []
    for number in range(count):
        entry = _unit(seed, number, "first-entry") * FIRST_ENTRY_SPREAD
        # The entrance is filled in when the map is known, see `advance_crowd`.
        visitors.append(
            Visitor(number, "away", entry, Position(0.0, 0.0), Position(0.0, 0.0), trip=0)
        )
    return Crowd(seed=seed, visitors=tuple(visitors))


TRAFFIC_REACH = 20.0
"""Distance within which a visitor counts as passing a spot, in `people_near`."""


class Traffic:
    """How many people pass spots on the map right now, with the visitors' positions worked out
    and sorted into squares of `TRAFFIC_REACH` once, for asking about many spots."""

    def __init__(self, world: World) -> None:
        self._weight = visitor_weight(world.time)
        self._squares: dict[tuple[int, int], list[Position]] = {}
        for visitor in world.crowd.visitors:
            there = visitor_position(visitor, world.time)
            if there is not None:
                square = (int(there.x // TRAFFIC_REACH), int(there.y // TRAFFIC_REACH))
                self._squares.setdefault(square, []).append(there)

    def near(self, centre: Position) -> float:
        """People passing `centre`: the visitors on the map within `TRAFFIC_REACH` of it, each
        standing for `visitor_weight` of them. Where the traffic is follows from where the
        visitors choose to go, so it moves with the layout of the town."""
        column, row = int(centre.x // TRAFFIC_REACH), int(centre.y // TRAFFIC_REACH)
        count = 0
        for x in (column - 1, column, column + 1):
            for y in (row - 1, row, row + 1):
                count += sum(
                    math.dist((there.x, there.y), (centre.x, centre.y)) <= TRAFFIC_REACH
                    for there in self._squares.get((x, y), ())
                )
        return count * self._weight


def people_near(world: World, centre: Position) -> float:
    """People passing a spot right now, see `Traffic`."""
    return Traffic(world).near(centre)


def visitor_weight(time: float) -> float:
    """People each visitor stands for at game time `time`; they grow in a straight line."""
    return 1 + time / VISITOR_GROWTH_SECONDS


def visitor_position(visitor: Visitor, time: float) -> Position | None:
    """Where the visitor is at game time `time`, or None while it is away from the map."""
    if visitor.phase == "away":
        return None
    if visitor.phase == "staying":
        return visitor.target
    walked = max(time - visitor.departs_at, 0.0) * VISITOR_SPEED
    for start, end in _legs(visitor):
        length = math.dist((start.x, start.y), (end.x, end.y))
        if walked <= length and length > 0:
            share = walked / length
            return Position(_along(start.x, end.x, share), _along(start.y, end.y, share))
        walked -= length
    return visitor.target


def next_visit(world: World) -> float | None:
    """Game time when the earliest walk, stay or absence of a visitor ends, or None when there
    are no visitors."""
    return min((visitor.until for visitor in world.crowd.visitors), default=None)


@dataclass(frozen=True)
class _Town:
    """What visitors can go to right now."""

    places: dict[str, dict[str, tuple[Position, int]]]
    """By kind of facility, then parcel: the centre and the pieces of content that draw people.
    Only ad slots and game halls showing content are in it, and every workspace."""
    kinds: Mapping[str, str]
    """The kind of facility on each parcel that has one."""


def advance_crowd(world: World) -> World:
    """Take the next step for every visitor whose walk, stay or absence has ended by
    `world.time`: count what the walkers who arrived have seen, take the fees of those who went
    into a workspace or game hall, then send everyone on. The same world when none has."""
    now = world.time
    due = [visitor for visitor in world.crowd.visitors if visitor.until <= now]
    if not due:
        return world
    town = _town(world)
    heading: Counter[str] = Counter(
        visitor.stop
        for visitor in world.crowd.visitors
        if visitor.stop is not None and visitor.phase != "away"
    )
    entrance = town_centre(world.map.size)
    views = dict(world.crowd.views)
    admissions = world.admissions
    moved: dict[int, Visitor] = {}
    for visitor in due:
        stepped, seen, entered = _step(world, visitor, entrance, town, heading)
        moved[visitor.id] = stepped
        for parcel_id, people in seen.items():
            views[parcel_id] = views.get(parcel_id, 0.0) + people
        if entered is not None:
            admissions = admit(
                admissions, entered, visitor_weight(visitor.until), FEES[town.kinds[entered]]
            )
    visitors = tuple(moved.get(visitor.id, visitor) for visitor in world.crowd.visitors)
    return replace(
        world,
        crowd=replace(world.crowd, visitors=visitors, views=tuple(sorted(views.items()))),
        admissions=admissions,
    )


def _step(
    world: World,
    visitor: Visitor,
    entrance: Position,
    town: _Town,
    heading: Counter[str],
) -> tuple[Visitor, dict[str, float], str | None]:
    """The visitor after the phase that has just ended, the people it has counted at each ad
    slot it saw, and the parcel of the workspace or game hall it has just paid to get into, if
    any. `heading` is how many visitors are going or standing at each place, and is kept up to
    date."""
    now = visitor.until
    seed = world.crowd.seed
    if visitor.phase == "away":
        working = _dice(seed, visitor, "route") < ROUTE_SHARE
        stops = MIN_STOPS + int(_dice(seed, visitor, "stops") * (MAX_STOPS - MIN_STOPS + 1))
        arrived = replace(
            visitor,
            phase="staying",
            at=entrance,
            target=entrance,
            legs=(),
            goal="exit",
            stop=None,
            trip=visitor.trip + 1,
            step=0,
            route=("workspace", "game_hall") if working else (),
            stops_left=0 if working else stops,
        )
        return _head_for(world, arrived, now, town, heading), {}, None
    if visitor.phase == "staying":
        if visitor.stop is not None:
            heading[visitor.stop] -= 1
        gone = replace(visitor, stop=None, goal="exit")
        return _head_for(world, gone, now, town, heading), {}, None
    # A walk has ended.
    kind = town.kinds[visitor.stop] if visitor.stop is not None else None
    dwell = 0.0
    if kind is not None:
        low, high = DWELL[kind]
        dwell = low + _dice(seed, visitor, "dwell") * (high - low)
    gained, seen = _count_views(visitor, now, dwell if kind == "ad" else 0.0, town.places["ad"])
    here = replace(visitor, at=visitor.target, legs=(), seen=seen)
    if visitor.goal == "wander":
        # Roaming uses up a stop too, so with nothing to see a visitor still leaves in the end.
        here = replace(here, stops_left=max(visitor.stops_left - 1, 0))
    if visitor.goal == "place":
        stays = replace(here, phase="staying", until=now + dwell)
        if kind == "ad":
            return replace(stays, stops_left=visitor.stops_left - 1), gained, None
        return stays, gained, visitor.stop
    if visitor.goal == "exit":
        gap = MIN_AWAY + _dice(seed, visitor, "away") * (MAX_AWAY - MIN_AWAY)
        return replace(here, phase="away", until=now + gap, stop=None), gained, None
    return _head_for(world, here, now, town, heading), gained, None


def _head_for(
    world: World,
    visitor: Visitor,
    now: float,
    town: _Town,
    heading: Counter[str],
) -> Visitor:
    """Send the visitor, standing at `visitor.target`, to the next place it picks, to the
    entrance when it has had enough, or roaming when it finds no ad slot to see. A visitor on a
    route goes to a facility for each stop on it, skipping those with nowhere to go."""
    here = visitor.target
    router = Router(world, here)
    goal: Goal = "exit"
    stop: str | None = None
    target = town_centre(world.map.size)
    rest = visitor.route
    if rest:
        left = list(rest)
        while left and stop is None:
            picked = _pick(world, visitor, left.pop(0), town, heading, router, now)
            if picked is not None:
                goal, (stop, target) = "place", picked
        rest = tuple(left)
    elif visitor.stops_left > 0:
        picked = _pick(world, visitor, "ad", town, heading, router, now)
        if picked is None:
            goal, target = "wander", _roam_from(world, visitor, here)
        else:
            goal, (stop, target) = "place", picked
    if stop is not None:
        heading[stop] += 1
    route = router.to(target)
    return replace(
        visitor,
        phase="walking",
        until=now + route.distance / VISITOR_SPEED,
        at=here,
        target=target,
        departs_at=now,
        legs=route.legs,
        goal=goal,
        stop=stop,
        route=rest,
        step=visitor.step + 1,
        seen=tuple((parcel_id, until) for parcel_id, until in visitor.seen if until > now),
    )


def _pick(
    world: World,
    visitor: Visitor,
    kind: str,
    town: _Town,
    heading: Counter[str],
    router: Router,
    now: float,
) -> tuple[str, Position] | None:
    """The facility of `kind` the visitor chooses, with its centre, by the logit of the module
    docstring; None when there is nothing it can go to. An ad slot it was counted at lately is
    out."""
    seed = world.crowd.seed
    recent = {parcel_id for parcel_id, until in visitor.seen if until > now}
    best: float | None = None
    chosen: tuple[str, Position] | None = None
    for parcel_id, (centre, pieces) in sorted(town.places[kind].items()):
        if kind == "ad" and parcel_id in recent:
            continue
        seconds = router.to(centre).distance / VISITOR_SPEED
        utility = (
            APPEAL * math.log(1 + pieces)
            - WALK_COST * seconds
            - CROWD_COST * heading[parcel_id] / CROWD_CAPACITY
            + _gumbel(_dice(seed, visitor, f"pick:{parcel_id}"))
        )
        if best is None or utility > best:
            best, chosen = utility, (parcel_id, centre)
    return chosen


def _count_views(
    visitor: Visitor,
    now: float,
    dwell: float,
    slots: dict[str, tuple[Position, int]],
) -> tuple[dict[str, float], tuple[tuple[str, float], ...]]:
    """The people the walk that has just ended adds to each ad slot's views, and the visitor's
    record of where it has been counted. A slot counts when the walk spent `MIN_VIEW_SECONDS`
    within `VIEW_RADIUS` of it, with the stay at its target added, and the visitor has not been
    counted there lately."""
    seconds: defaultdict[str, float] = defaultdict(float)
    for start, end in _legs(visitor):
        for parcel_id, (centre, _) in slots.items():
            seconds[parcel_id] += _inside(start, end, centre, VIEW_RADIUS) / VISITOR_SPEED
    if visitor.stop is not None and visitor.stop in slots:
        seconds[visitor.stop] += dwell
    recent = {parcel_id: until for parcel_id, until in visitor.seen if until > now}
    gained: dict[str, float] = {}
    for parcel_id, spent in sorted(seconds.items()):
        if spent >= MIN_VIEW_SECONDS and parcel_id not in recent:
            gained[parcel_id] = visitor_weight(now)
            recent[parcel_id] = now + SEEN_FOR
    return gained, tuple(sorted(recent.items()))


def _inside(start: Position, end: Position, centre: Position, radius: float) -> float:
    """Length of the straight walk from `start` to `end` that lies within `radius` of
    `centre`."""
    dx, dy = end.x - start.x, end.y - start.y
    length = math.hypot(dx, dy)
    if length == 0:
        return 0.0
    fx, fy = start.x - centre.x, start.y - centre.y
    # |start + u * (end - start) - centre|^2 = radius^2, a quadratic in u.
    a = length * length
    b = 2 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - radius * radius
    discriminant = b * b - 4 * a * c
    if discriminant <= 0:
        return 0.0
    root = math.sqrt(discriminant)
    enter, leave = (-b - root) / (2 * a), (-b + root) / (2 * a)
    return max(min(leave, 1.0) - max(enter, 0.0), 0.0) * length


def _legs(visitor: Visitor) -> list[tuple[Position, Position]]:
    """The straight stretches of the visitor's present walk: one, or several when it turns at
    the ends of bridges or goes through a network of portals, which it crosses in no time."""
    return list(visitor.legs) or [(visitor.at, visitor.target)]


def _town(world: World) -> _Town:
    places: dict[str, dict[str, tuple[Position, int]]] = {
        "ad": {},
        "workspace": {},
        "game_hall": {},
    }
    kinds = {}
    for parcel in world.parcels:
        facility = parcel.facility
        if facility is None:
            continue
        kinds[parcel.id] = facility.kind
        if facility.kind == "workspace":
            pieces = WORKSPACE_PIECES
        elif facility.kind in ("ad", "game_hall"):
            pieces = shown(facility, world.time)
        else:
            continue
        if pieces:
            places[facility.kind][parcel.id] = (
                parcel_centre(world.map, parcel.col, parcel.row),
                pieces,
            )
    return _Town(places, kinds)


def _roam_from(world: World, visitor: Visitor, here: Position) -> Position:
    """A random spot `MIN_ROAM` to `MAX_ROAM` units from `here`, turned back from the edge of the
    map, and kept on the island it is on where the map has islands: roaming never crosses a
    bridge."""
    seed = world.crowd.seed
    angle = 2 * math.pi * _dice(seed, visitor, "roam-angle")
    far = MIN_ROAM + _dice(seed, visitor, "roam-far") * (MAX_ROAM - MIN_ROAM)
    size = world.map.size
    x, y = here.x + far * math.cos(angle), here.y + far * math.sin(angle)
    if not 0 <= x <= size:
        x = here.x - (x - here.x)
    if not 0 <= y <= size:
        y = here.y - (y - here.y)
    spot = Position(min(max(x, 0.0), size), min(max(y, 0.0), size))
    return on_island(world.map, here, spot)


def _along(start: float, end: float, share: float) -> float:
    # Kept between the ends, so rounding never steps off the map.
    return min(max(start + (end - start) * share, min(start, end)), max(start, end))


def _dice(seed: int, visitor: Visitor, what: str) -> float:
    """A number in (0, 1) fixed by the seed, the visitor, its trip and step, and `what`."""
    return _unit(seed, visitor.id, f"{visitor.trip}/{visitor.step}/{what}")


def _unit(seed: int, number: int, what: str) -> float:
    digest = hashlib.blake2b(f"{seed}/{number}/{what}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") + 0.5) / 2**64


def _gumbel(unit: float) -> float:
    """Gumbel noise from a number in (0, 1): choosing the largest utility plus this picks each
    option with the probability of the multinomial logit."""
    return -math.log(-math.log(unit))
