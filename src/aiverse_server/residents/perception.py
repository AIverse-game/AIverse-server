"""Perception: what a resident notices when they start a round of work.

Whoever drives a resident, such as the AI, calls `app.Game.begin_turn` at the start of each
round before choosing what to do; it brings the game time up to now and then calls `begin_turn`
here. The resident looks around within `PERCEPTION_RADIUS` of where they stand and notices:

- the other residents in range, as they are right now, walkers where they have got to on their
  way (see `walking.py`);
- what others did in range since the resident last looked, from the world's activity log.

Other residents are not remembered, since their positions are public anyway (see the
observation rules in the design document of the client repository). Range is measured from
where the perceiver stands now, not where they stood when something happened; a move is in
range when either end is.

Each look also hands back the notes the resident chose to keep, long and short term, with
the expired ones cleared first, see `memory.py`, and what was said in their hearing since
they last looked, their own words included, see `speech.py`, and the server's notices to
everyone, see `lifecycle.announce`.

`record_activity` fills the log by comparing the world before and after a change.
`actions.perform` and the server's clock call it, so every walk and departure is logged
however it came about. The log keeps the latest `ACTIVITY_LIMIT` entries, so a resident who
looks rarely may miss the oldest.

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
    Notice,
    Position,
    Speech,
    World,
)
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
    walking_to: Position | None
    """Where they are walking to, or None."""
    left: bool


@dataclass(frozen=True)
class Perception:
    """What a resident noticed at the start of a round."""

    time: float
    position: Position
    """Where the resident was when looking."""
    residents: tuple[ResidentSighting, ...]
    """Other residents in range right now."""
    activity: tuple[Activity, ...]
    """What others did in range since the resident last looked, oldest first."""
    long_term: tuple[Note, ...]
    """The notes the resident keeps for the whole game, oldest first, see `memory.py`."""
    short_term: tuple[Note, ...]
    """The short-term notes not yet expired, oldest first."""
    speech: tuple[Speech, ...]
    """What the resident said or heard since they last looked, oldest first; who heard was
    settled when it was said, see `speech.py`."""
    notices: tuple[Notice, ...] = ()
    """What the server announced to everyone since the resident last looked, oldest first."""


def begin_turn(world: World, resident_id: str) -> tuple[World, Perception]:
    """Look around at the start of a round: returns the world with the resident's memory
    brought up to date, and what they noticed. Refused with `GameError` for a resident who
    does not exist or has left, and once the game is over; a walker can still look."""
    check_can_act(world, resident_id)
    me = next(resident for resident in world.residents if resident.id == resident_id)
    here = position_at(me, world.time)
    residents = tuple(
        ResidentSighting(
            resident_id=other.id,
            name=other.name,
            position=there,
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
    notices = tuple(
        notice for notice in world.notices.entries if notice.seq > me.memory.notices_seen
    )
    memory = replace(
        forget_expired(me.memory, world.time),
        activity_seen=world.activity.next_seq - 1,
        speech_seen=world.speech.next_seq - 1,
        notices_seen=world.notices.next_seq - 1,
    )
    residents_after = tuple(
        replace(resident, memory=memory) if resident.id == resident_id else resident
        for resident in world.residents
    )
    perception = Perception(
        time=world.time,
        position=here,
        residents=residents,
        activity=activity,
        long_term=memory.long_term,
        short_term=memory.short_term,
        speech=speech,
        notices=notices,
    )
    return replace(world, residents=residents_after), perception


def record_activity(before: World, after: World) -> World:
    """Log, at `after.time`, what residents did between the two worlds: set off on a walk,
    moved (arrived, or stopped on the way), or left the game. Returns `after` itself when there
    is nothing to log."""
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
