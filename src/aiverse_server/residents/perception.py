"""Perception and discovery: what a resident notices when they start a round of work.

Whoever drives a resident, such as the AI that will, calls `app.Game.begin_turn` at the
start of each round before choosing what to do; it brings the game time up to now and then
calls `begin_turn` here. The resident looks around within `PERCEPTION_RADIUS`
of where they stand and notices:

- the pools and other residents in range, as they are right now, walkers where they have got
  to on their way (see `walking.py`);
- what others did in range since the resident last looked, from the world's activity log.

Seeing a pool discovers it: the resident remembers where it is, and its reserve as last
seen, after walking away (`Resident.memory`). That holds on the way too: a walk discovers
every pool within `PERCEPTION_RADIUS` of the line walked, on either side, not only those near
where it starts or ends. A resident who looks while still walking has seen the stretch
walked so far; the whole line is settled when the walk ends, see `record_discoveries`, and
what it turned up is reported by the next look in `Perception.discovered`. Passing a pool
already known refreshes nothing, as what it held at that moment is not known. Only pools are
found this way: residents and their activity are noticed from where one stands, since the
world keeps no history of where they were. Other residents are not remembered, since
their positions are public anyway (see the observation rules in the design document of the
client repository). Range is measured from where the perceiver stands now, not where they
stood when something happened; a move is in range when either end is.

The market is in sight wherever the resident stands: its place and price are public, like a
price board in town, see `market.py`, and so are the chips on sale there, see
`chips.py`.

Each look also hands back the notes the resident chose to keep, long and short term, with
the expired ones cleared first, see `memory.py`, and what was said in their hearing since
they last looked, their own words included, see `speech.py`.

`record_activity` fills the log by comparing the world before and after a change.
`actions.perform` and the server's clock call it, so every walk, mining job and departure is
logged however it came about. The log keeps the latest `ACTIVITY_LIMIT` entries, so a
resident who looks rarely may miss the oldest.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from dataclasses import dataclass, replace

from ..core.world import (
    ACTIVITY_LIMIT,
    Activity,
    ActivityLog,
    Note,
    PoolSighting,
    Position,
    Speech,
    World,
)
from ..economy.chips import CHIP_MODELS, ChipModel
from ..economy.market import unit_price
from ..game.lifecycle import check_can_act
from .memory import forget_expired
from .walking import position_at

PERCEPTION_RADIUS = 20.0
"""How far, in map units, a resident notices things around them."""


@dataclass(frozen=True)
class ResidentSighting:
    """Another resident in range, as they are right now."""

    resident_id: str
    name: str
    position: Position
    mining_pool_id: str | None
    """The pool they are busy mining, or None."""
    walking_to: Position | None
    """Where they are walking to, or None."""
    left: bool


@dataclass(frozen=True)
class MarketSighting:
    """The market as it is right now."""

    position: Position
    radius: float
    price: float
    """Coins one more token fetches right now, before rounding; it falls as tokens are sold
    and recovers with time."""
    chips: tuple[ChipModel, ...]
    """Chips on sale here at fixed prices, worst first."""


@dataclass(frozen=True)
class Perception:
    """What a resident noticed at the start of a round."""

    time: float
    position: Position
    """Where the resident was when looking."""
    pools: tuple[PoolSighting, ...]
    """Pools in range right now."""
    residents: tuple[ResidentSighting, ...]
    """Other residents in range right now."""
    activity: tuple[Activity, ...]
    """What others did in range since the resident last looked, oldest first."""
    discovered: tuple[str, ...]
    """Ids of the pools seen for the first time since the last look, from where the resident
    stands, along the stretch walked, or on walks finished meanwhile."""
    known_pools: tuple[PoolSighting, ...]
    """Every pool discovered so far, including this round's, at its latest sighting."""
    long_term: tuple[Note, ...]
    """The notes the resident keeps for the whole game, oldest first, see `memory.py`."""
    short_term: tuple[Note, ...]
    """The short-term notes not yet expired, oldest first."""
    market: MarketSighting | None
    """The market, wherever the resident stands; None on a map without one."""
    speech: tuple[Speech, ...]
    """What the resident said or heard since they last looked, oldest first; who heard was
    settled when it was said, see `speech.py`."""


def begin_turn(world: World, resident_id: str) -> tuple[World, Perception]:
    """Look around at the start of a round: returns the world with the resident's memory
    brought up to date, and what they noticed. Refused with `GameError` for a resident who
    does not exist or has left, and once the game is over; a busy miner can still look."""
    check_can_act(world, resident_id)
    me = next(resident for resident in world.residents if resident.id == resident_id)
    here = position_at(me, world.time)
    pools = tuple(
        PoolSighting(pool.id, pool.position, pool.radius, pool.reserve, world.time)
        for pool in world.pools
        if _in_range(here, pool.position)
    )
    residents = tuple(
        ResidentSighting(
            resident_id=other.id,
            name=other.name,
            position=there,
            mining_pool_id=other.mining.pool_id if other.mining is not None else None,
            walking_to=other.walking.target if other.walking is not None else None,
            left=other.left,
        )
        for other in world.residents
        if other.id != resident_id and _in_range(here, there := position_at(other, world.time))
    )
    activity = tuple(
        entry
        for entry in world.activity.entries
        if entry.seq > me.memory.activity_seen
        and entry.resident_id != resident_id
        and (
            _in_range(here, entry.position)
            or (entry.origin is not None and _in_range(here, entry.origin))
        )
    )
    speech = tuple(
        line
        for line in world.speech.entries
        if line.seq > me.memory.speech_seen
        and (line.speaker_id == resident_id or resident_id in line.listeners)
    )
    known = {sighting.pool_id: sighting for sighting in me.memory.known_pools}
    # The stretch walked so far, if walking, may turn up pools not seen before.
    passed = _unknown_along(world, known, me.position, here)
    discovered = tuple(
        dict.fromkeys(
            (
                *me.memory.found_on_walks,
                *(s.pool_id for s in (*passed, *pools) if s.pool_id not in known),
            )
        )
    )
    known.update((sighting.pool_id, sighting) for sighting in (*passed, *pools))
    memory = replace(
        forget_expired(me.memory, world.time),
        known_pools=tuple(known.values()),
        found_on_walks=(),
        activity_seen=world.activity.next_seq - 1,
        speech_seen=world.speech.next_seq - 1,
    )
    residents_after = tuple(
        replace(resident, memory=memory) if resident.id == resident_id else resident
        for resident in world.residents
    )
    perception = Perception(
        time=world.time,
        position=here,
        pools=pools,
        residents=residents,
        activity=activity,
        discovered=discovered,
        known_pools=memory.known_pools,
        long_term=memory.long_term,
        short_term=memory.short_term,
        market=None
        if world.market is None
        else MarketSighting(
            world.market.position,
            world.market.radius,
            unit_price(world.market, world.time),
            CHIP_MODELS,
        ),
        speech=speech,
    )
    return replace(world, residents=residents_after), perception


def record_activity(before: World, after: World) -> World:
    """Log, at `after.time`, what residents did between the two worlds: set off on a walk,
    moved (arrived, or stopped on the way), started or finished mining, or left the game.
    Mining that stops because the miner left is logged as leaving only. Returns `after`
    itself when there is nothing to log."""
    previous = {resident.id: resident for resident in before.residents}
    # Numbered once all are found; 0 is a placeholder.
    found: list[Activity] = []
    for resident in after.residents:
        old = previous.get(resident.id)
        if old is None or old.left:
            continue
        now, who, here = after.time, resident.id, resident.position
        if old.walking is None and resident.walking is not None:
            target = resident.walking.target
            found.append(Activity(0, now, who, "started_walking", target, origin=here))
        if here != old.position:
            found.append(Activity(0, now, who, "moved", here, origin=old.position))
        if old.mining is None and resident.mining is not None:
            pool_id = resident.mining.pool_id
            found.append(Activity(0, now, who, "started_mining", here, pool_id=pool_id))
        if old.mining is not None and resident.mining is None and not resident.left:
            pool_id = old.mining.pool_id
            found.append(Activity(0, now, who, "finished_mining", here, pool_id=pool_id))
        if resident.left:
            found.append(Activity(0, now, who, "left_game", here))
    if not found:
        return after
    log = after.activity
    numbered = tuple(replace(entry, seq=log.next_seq + i) for i, entry in enumerate(found))
    entries = (*log.entries, *numbered)[-ACTIVITY_LIMIT:]
    return replace(after, activity=ActivityLog(entries, log.next_seq + len(numbered)))


def record_discoveries(before: World, after: World) -> World:
    """Discover, for each resident whose walk ended between the two worlds, the pools within
    `PERCEPTION_RADIUS` of the line they walked that they did not know: they are remembered
    as of `after.time` and held for the resident's next look to report. Residents who left
    are skipped. Returns `after` itself when nothing was found."""
    previous = {resident.id: resident for resident in before.residents}
    residents = list(after.residents)
    for index, resident in enumerate(residents):
        old = previous.get(resident.id)
        if old is None or old.walking is None or resident.walking is not None or resident.left:
            continue
        memory = resident.memory
        known = {sighting.pool_id: sighting for sighting in memory.known_pools}
        sighted = _unknown_along(after, known, old.position, resident.position)
        if not sighted:
            continue
        known.update((sighting.pool_id, sighting) for sighting in sighted)
        found = (*memory.found_on_walks, *(sighting.pool_id for sighting in sighted))
        memory = replace(memory, known_pools=tuple(known.values()), found_on_walks=found)
        residents[index] = replace(resident, memory=memory)
    if all(new is old for new, old in zip(residents, after.residents, strict=True)):
        return after
    return replace(after, residents=tuple(residents))


def _unknown_along(
    world: World, known: dict[str, PoolSighting], start: Position, end: Position
) -> tuple[PoolSighting, ...]:
    """The pools within range of the segment from `start` to `end` that are not in `known`,
    as they are now. Known pools are left as last seen: passing one on the way refreshes
    nothing, since what it held at that moment is not known."""
    return tuple(
        PoolSighting(pool.id, pool.position, pool.radius, pool.reserve, world.time)
        for pool in world.pools
        if pool.id not in known
        and _distance_to_segment(pool.position, start, end) <= PERCEPTION_RADIUS
    )


def _distance_to_segment(point: Position, start: Position, end: Position) -> float:
    dx, dy = end.x - start.x, end.y - start.y
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.dist((point.x, point.y), (start.x, start.y))
    share = ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_squared
    share = min(max(share, 0.0), 1.0)
    return math.dist((point.x, point.y), (start.x + dx * share, start.y + dy * share))


def _in_range(here: Position, there: Position) -> bool:
    return math.dist((here.x, here.y), (there.x, there.y)) <= PERCEPTION_RADIUS
