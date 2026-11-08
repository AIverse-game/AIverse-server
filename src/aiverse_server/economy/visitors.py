"""Visitors: scripted tourists who walk the map and see the ad slots, so that where the traffic
goes is up to the layout of the town and not fixed beforehand.

There are `VISITOR_COUNT` of them, each standing for more and more people as the game goes on,
`visitor_weight`. A visitor comes in through the entrance, the middle of the map, makes a
few stops at ad slots showing content, then walks back out and stays away a while before
coming again. Nothing about it is stored beyond where it set off from and to, and when: where
it is in between is worked out from that, `visitor_position`, and something happens only when a
walk or a stay ends, `advance_crowd`, at the times `next_visit` gives.

Where to next, `_head_for`: among the ad slots showing content that the visitor has not been
counted at lately, it picks by a multinomial logit, so every slot has a chance but better ones
more. A slot's utility is `APPEAL` times the log of one plus the pieces it shows, less
`WALK_COST` per second of walking and `CROWD_COST` per `CROWD_CAPACITY` visitors already going
or standing there, plus noise. The walk is the shorter of walking straight and going through a
network of portals, see `portals.Router`, so the lines change which slots are near, and whether
a visitor takes one is its own choice. Visitors use the lines for nothing. With nothing worth
seeing the visitor roams to a random spot nearby and looks again.

Choices are random but the dice are not stateful: each is a hash of the crowd's seed, the
visitor, its trip, its step and what is being chosen, so the same game plays out the same way.

A visitor counts as having seen a slot when it spends at least `MIN_VIEW_SECONDS` within
`VIEW_RADIUS` of it, standing there or passing by, and is not counted at the same slot again for
`SEEN_FOR` game seconds, even on a later trip. The count is made when the walk ends, judging
by the slots showing content then, and adds the people the visitor
stands for to the views of the slot's parcel, `Crowd.views`. Nothing is paid for them yet.

Like the rest of the world, every function returns a new world and leaves its argument
untouched.
"""

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import replace

from ..core.land import Parcel
from ..core.visitors import Crowd, Goal, Visitor
from ..core.world import Position, World
from .ads import shown
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
"""Game seconds a visitor stays at a slot it has come to see, least and most."""

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


def advance_crowd(world: World) -> World:
    """Take the next step for every visitor whose walk, stay or absence has ended by
    `world.time`: count what the walkers who arrived have seen, then send everyone on. The same
    world when none has."""
    now = world.time
    due = [visitor for visitor in world.crowd.visitors if visitor.until <= now]
    if not due:
        return world
    slots = _open_slots(world)
    heading: Counter[str] = Counter(
        visitor.stop
        for visitor in world.crowd.visitors
        if visitor.stop is not None and visitor.phase != "away"
    )
    entrance = town_centre(world.map.size)
    views = dict(world.crowd.views)
    moved: dict[int, Visitor] = {}
    for visitor in due:
        stepped, seen = _step(world, visitor, entrance, slots, heading)
        moved[visitor.id] = stepped
        for parcel_id, people in seen.items():
            views[parcel_id] = views.get(parcel_id, 0.0) + people
    visitors = tuple(moved.get(visitor.id, visitor) for visitor in world.crowd.visitors)
    return replace(
        world, crowd=replace(world.crowd, visitors=visitors, views=tuple(sorted(views.items())))
    )


def _step(
    world: World,
    visitor: Visitor,
    entrance: Position,
    slots: dict[str, tuple[Position, int]],
    heading: Counter[str],
) -> tuple[Visitor, dict[str, float]]:
    """The visitor after the phase that has just ended, and the people it has counted at
    each slot it saw. `heading` is how many visitors are going or standing at each slot, and is
    kept up to date."""
    now = visitor.until
    seed = world.crowd.seed
    if visitor.phase == "away":
        stops = MIN_STOPS + int(_dice(seed, visitor, "stops") * (MAX_STOPS - MIN_STOPS + 1))
        arrived = replace(
            visitor,
            phase="staying",
            at=entrance,
            target=entrance,
            via=None,
            goal="exit",
            stop=None,
            trip=visitor.trip + 1,
            step=0,
            stops_left=stops,
        )
        return _head_for(world, arrived, now, slots, heading), {}
    if visitor.phase == "staying":
        if visitor.stop is not None:
            heading[visitor.stop] -= 1
        gone = replace(visitor, stop=None, goal="exit")
        return _head_for(world, gone, now, slots, heading), {}
    # A walk has ended.
    dwell = 0.0
    if visitor.goal == "slot":
        dwell = MIN_DWELL + _dice(seed, visitor, "dwell") * (MAX_DWELL - MIN_DWELL)
    gained, seen = _count_views(visitor, now, dwell, slots)
    here = replace(visitor, at=visitor.target, via=None, seen=seen)
    if visitor.goal == "wander":
        # Roaming uses up a stop too, so with nothing to see a visitor still leaves in the end.
        here = replace(here, stops_left=max(visitor.stops_left - 1, 0))
    if visitor.goal == "slot":
        return (
            replace(here, phase="staying", until=now + dwell, stops_left=visitor.stops_left - 1),
            gained,
        )
    if visitor.goal == "exit":
        gap = MIN_AWAY + _dice(seed, visitor, "away") * (MAX_AWAY - MIN_AWAY)
        return replace(here, phase="away", until=now + gap, stop=None), gained
    return _head_for(world, here, now, slots, heading), gained


def _head_for(
    world: World,
    visitor: Visitor,
    now: float,
    slots: dict[str, tuple[Position, int]],
    heading: Counter[str],
) -> Visitor:
    """Send the visitor, standing at `visitor.target`, to the next ad slot it picks, to the
    entrance when it has seen enough, or roaming when there is nothing to see."""
    here = visitor.target
    seed = world.crowd.seed
    router = Router(world, here)
    goal: Goal = "exit"
    stop: str | None = None
    target = town_centre(world.map.size)
    if visitor.stops_left > 0:
        recent = {parcel_id for parcel_id, until in visitor.seen if until > now}
        best: float | None = None
        for parcel_id, (centre, pieces) in sorted(slots.items()):
            if parcel_id in recent:
                continue
            seconds = router.to(centre).distance / VISITOR_SPEED
            utility = (
                APPEAL * math.log(1 + pieces)
                - WALK_COST * seconds
                - CROWD_COST * heading[parcel_id] / CROWD_CAPACITY
                + _gumbel(_dice(seed, visitor, f"pick:{parcel_id}"))
            )
            if best is None or utility > best:
                best, goal, stop, target = utility, "slot", parcel_id, centre
        if stop is None:
            goal, target = "wander", _roam_from(world, visitor, here)
        else:
            heading[stop] += 1
    route = router.to(target)
    return replace(
        visitor,
        phase="walking",
        until=now + route.distance / VISITOR_SPEED,
        at=here,
        target=target,
        departs_at=now,
        via=route.via,
        goal=goal,
        stop=stop,
        step=visitor.step + 1,
        seen=tuple((parcel_id, until) for parcel_id, until in visitor.seen if until > now),
    )


def _count_views(
    visitor: Visitor,
    now: float,
    dwell: float,
    slots: dict[str, tuple[Position, int]],
) -> tuple[dict[str, float], tuple[tuple[str, float], ...]]:
    """The people the walk that has just ended adds to each slot's views, and the visitor's
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
    """The straight stretches of the visitor's present walk: one, or two when it goes through
    a network of portals, which it crosses in no time."""
    if visitor.via is None:
        return [(visitor.at, visitor.target)]
    return [(visitor.at, visitor.via[0]), (visitor.via[1], visitor.target)]


def _open_slots(world: World) -> dict[str, tuple[Position, int]]:
    """Every ad slot showing content now, by parcel, with its centre and the pieces shown."""
    open_slots = {}
    for parcel in world.parcels:
        pieces = _pieces(parcel, world.time)
        if pieces:
            open_slots[parcel.id] = (parcel_centre(world.map.size, parcel.col, parcel.row), pieces)
    return open_slots


def _pieces(parcel: Parcel, time: float) -> int:
    facility = parcel.facility
    if facility is None or facility.kind != "ad":
        return 0
    return shown(facility, time)


def _roam_from(world: World, visitor: Visitor, here: Position) -> Position:
    """A random spot `MIN_ROAM` to `MAX_ROAM` units from `here`, turned back from the edge."""
    seed = world.crowd.seed
    angle = 2 * math.pi * _dice(seed, visitor, "roam-angle")
    far = MIN_ROAM + _dice(seed, visitor, "roam-far") * (MAX_ROAM - MIN_ROAM)
    size = world.map.size
    x, y = here.x + far * math.cos(angle), here.y + far * math.sin(angle)
    if not 0 <= x <= size:
        x = here.x - (x - here.x)
    if not 0 <= y <= size:
        y = here.y - (y - here.y)
    return Position(min(max(x, 0.0), size), min(max(y, 0.0), size))


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
