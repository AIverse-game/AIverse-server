"""Walking: residents cross the map at a fixed speed instead of arriving at once.

`MoveTo` starts a walk in a straight line at `WALK_SPEED`. The resident pays for the whole
walk when setting off, see `energy.walk_cost`, and arrives `walk_seconds` of game time later.
Until then they are busy, as when harvesting: they can do nothing but talk or leave the game.
During the walk `Resident.position` stays where they set off; `position_at` tells where they
are on the way, which is what others see. Leaving the game, or the game ending, stops them
there.

Game time is `World.time`. The server moves it on as real time passes and calls
`finish_walks` to bring in those who have arrived; `next_arrival` tells it when to look again.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from collections.abc import Callable
from dataclasses import replace

from ..core.world import Position, Resident, WalkJob, World

WALK_SPEED = 2.5
"""Map units a resident walks per game second. A spring is about 20 units from the nearest
spot on the default map, some 8 seconds away, close to the 10 seconds one harvesting job takes;
crossing the whole map takes 40."""


def walk_seconds(start: Position, end: Position) -> float:
    """Game seconds a straight walk from `start` to `end` takes."""
    return math.dist((start.x, start.y), (end.x, end.y)) / WALK_SPEED


def position_at(resident: Resident, time: float) -> Position:
    """Where the resident is at game time `time`: on the line to their target while walking."""
    walk = resident.walking
    if walk is None:
        return resident.position
    span = walk.arrives_at - walk.departs_at
    if span <= 0 or time >= walk.arrives_at:
        return walk.target
    share = max(time - walk.departs_at, 0.0) / span
    start, end = resident.position, walk.target

    def along(a: float, b: float) -> float:
        # Kept between the ends, so rounding never steps off the map.
        return min(max(a + (b - a) * share, min(a, b)), max(a, b))

    return Position(along(start.x, end.x), along(start.y, end.y))


def start_walk(world: World, resident_id: str, target: Position) -> World:
    """Set the resident off towards `target`; standing still takes no time and starts nothing.
    The caller checks the target and charges the walk."""
    resident = _find(world, resident_id)
    seconds = walk_seconds(resident.position, target)
    if seconds == 0:
        return world
    walk = WalkJob(target=target, departs_at=world.time, arrives_at=world.time + seconds)
    return _with(world, resident_id, lambda r: replace(r, walking=walk))


def finish_walks(world: World) -> World:
    """Bring in everyone who has arrived by `world.time`; the same world when nobody has."""
    arrived = [
        resident.id
        for resident in world.residents
        if resident.walking is not None and resident.walking.arrives_at <= world.time
    ]
    for resident_id in arrived:
        world = _with(world, resident_id, _arrive)
    return world


def stop_walking(world: World, resident_id: str) -> World:
    """Stop the resident where they are now, if walking."""
    resident = _find(world, resident_id)
    if resident.walking is None:
        return world
    here = position_at(resident, world.time)
    return _with(world, resident_id, lambda r: replace(r, position=here, walking=None))


def next_arrival(world: World) -> float | None:
    """Game time when the earliest walk ends, or None when nobody is walking."""
    ends = [r.walking.arrives_at for r in world.residents if r.walking is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    return next(resident for resident in world.residents if resident.id == resident_id)


def _arrive(resident: Resident) -> Resident:
    if resident.walking is None:
        return resident
    return replace(resident, position=resident.walking.target, walking=None)


def _with(world: World, resident_id: str, change: Callable[[Resident], Resident]) -> World:
    residents = tuple(
        change(resident) if resident.id == resident_id else resident for resident in world.residents
    )
    return replace(world, residents=residents)
