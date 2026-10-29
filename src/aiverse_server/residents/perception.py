"""Perception and discovery: what a resident notices when they start a round of work.

Whoever drives a resident, such as the AI that will, calls `app.Game.begin_turn` at the
start of each round before choosing what to do; it brings the game time up to now and then
calls `begin_turn` here. The resident looks around within `PERCEPTION_RADIUS`
of where they stand and notices:

- the pools and other residents in range, as they are right now, walkers where they have got
  to on their way (see `walking.py`);
- what others did in range since the resident last looked, from the world's activity log.

Seeing a pool discovers it: the resident remembers where it is, and its reserve as last
seen, after walking away (`Resident.memory`). Other residents are not remembered, since
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
    """Ids of the pools seen for the first time this round."""
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
    discovered = tuple(sighting.pool_id for sighting in pools if sighting.pool_id not in known)
    known.update((sighting.pool_id, sighting) for sighting in pools)
    memory = replace(
        forget_expired(me.memory, world.time),
        known_pools=tuple(known.values()),
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


def _in_range(here: Position, there: Position) -> bool:
    return math.dist((here.x, here.y), (there.x, there.y)) <= PERCEPTION_RADIUS
