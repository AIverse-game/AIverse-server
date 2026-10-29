"""Authoritative world state: a finite square map with a continuous coordinate system.

Coordinates: the origin (0, 0) is the north-west (top-left) corner, x grows east
(right) and y grows south (down), matching screen space. Both axes cover the closed
range [0, size]. Positions are real numbers, not grid cells: residents, token pools, the
market and later other places can stand anywhere inside the map.
"""

import math
import re
from dataclasses import dataclass
from typing import Literal

from .events import EventQueue

COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")


@dataclass(frozen=True)
class Position:
    x: float
    y: float


@dataclass(frozen=True)
class Inventory:
    """Items a resident carries: a count per item kind, every count at least 1.

    Capacity is unlimited for now. Items belong to one game only: residents start with
    empty inventories, and nothing outlives the in-memory world.
    """

    stacks: tuple[tuple[str, int], ...] = ()
    """(item kind, count) pairs in any order; stored sorted by kind, so equal contents
    compare equal."""

    def __post_init__(self) -> None:
        kinds = [kind for kind, _ in self.stacks]
        if len(set(kinds)) != len(kinds):
            raise ValueError("inventory item kinds must be unique")
        for kind, count in self.stacks:
            if not kind:
                raise ValueError("item kind must not be empty")
            # Exactly int: bool is an int subclass, and floats such as NaN slip past `< 1`.
            if type(count) is not int or count < 1:
                raise ValueError(f"item {kind!r} has count {count!r}, must be an integer >= 1")
        object.__setattr__(self, "stacks", tuple(sorted(self.stacks)))

    def count(self, kind: str) -> int:
        return dict(self.stacks).get(kind, 0)

    def counts(self) -> dict[str, int]:
        """A fresh copy, sorted by kind; changing it leaves the inventory untouched."""
        return dict(self.stacks)


@dataclass(frozen=True)
class MiningJob:
    """A resident mining a pool, see `mining.py`; they can do nothing else until it ends."""

    pool_id: str
    ends_at: float
    """Game time when the tokens are paid out, see `World.time`."""
    amount: int
    """Tokens taken out of the pool when the job started, paid to the miner when it ends."""
    chip: str
    """Model of the chip doing the work, see `chips.py`; the miner holds it until the job
    ends."""


@dataclass(frozen=True)
class WalkJob:
    """A resident on their way to `target`, see `walking.py`; they can do nothing else until
    they arrive. Their `Resident.position` stays where they set off until then."""

    target: Position
    departs_at: float
    arrives_at: float
    """Game times when the walk began and when it ends, see `World.time`."""


@dataclass(frozen=True)
class PoolSighting:
    """A pool as a resident last saw it, see `perception.py`."""

    pool_id: str
    position: Position
    radius: float
    reserve: int
    """Tokens left when it was seen; the pool may hold fewer by now."""
    seen_at: float
    """Game time of the sighting, see `World.time`."""


NoteTerm = Literal["long_term", "short_term"]
"""How long a note lasts: the whole game, or `memory.SHORT_TERM_SECONDS`."""

NOTE_LENGTH_LIMIT = 300
"""Characters a note may hold, so a resident's notes stay small enough to read every round."""

NOTE_LIMITS: dict[NoteTerm, int] = {"long_term": 30, "short_term": 20}
"""Notes a resident may keep of each term at once; one must be forgotten to write another."""


@dataclass(frozen=True)
class Note:
    """Something a resident chose to remember, see `memory.py`."""

    id: int
    """Unique among the resident's notes for the whole game, counting up from 1."""
    text: str
    written_at: float
    """Game time when it was written, see `World.time`."""
    expires_at: float | None
    """Game time when a short-term note is cleared; None for a long-term one."""


@dataclass(frozen=True)
class Memory:
    """What a resident has noticed so far, see `perception.py`, and the notes they chose to
    keep, see `memory.py`. Empty when the game starts."""

    known_pools: tuple[PoolSighting, ...] = ()
    """Every pool the resident has discovered, at its latest sighting, ordered by pool id."""
    activity_seen: int = 0
    """Sequence number of the last activity entry already looked at; 0 before the first look."""
    long_term: tuple[Note, ...] = ()
    """Notes kept for the rest of the game, oldest first."""
    short_term: tuple[Note, ...] = ()
    """Notes cleared once they expire, oldest first; expired ones may linger until the memory
    is next used, see `memory.forget_expired`."""
    next_note_id: int = 1
    """Id the next note gets; it only counts up, so ids are never reused within a game."""

    def __post_init__(self) -> None:
        ids = [sighting.pool_id for sighting in self.known_pools]
        if len(set(ids)) != len(ids):
            raise ValueError("known pools must be unique")
        object.__setattr__(
            self, "known_pools", tuple(sorted(self.known_pools, key=lambda s: s.pool_id))
        )
        if type(self.activity_seen) is not int or self.activity_seen < 0:
            raise ValueError(f"activity seen must be an integer >= 0, got {self.activity_seen!r}")
        if type(self.next_note_id) is not int or self.next_note_id < 1:
            raise ValueError(f"next note id must be an integer >= 1, got {self.next_note_id!r}")
        note_ids: set[int] = set()
        terms: tuple[tuple[NoteTerm, tuple[Note, ...]], ...] = (
            ("long_term", self.long_term),
            ("short_term", self.short_term),
        )
        for term, notes in terms:
            if len(notes) > NOTE_LIMITS[term]:
                raise ValueError(f"memory holds {len(notes)} {term} notes, over the limit")
            for note in notes:
                if type(note.id) is not int or not 1 <= note.id < self.next_note_id:
                    raise ValueError(
                        f"note id {note.id!r} must be an integer from 1 to {self.next_note_id - 1}"
                    )
                if note.id in note_ids:
                    raise ValueError(f"note id {note.id} is used twice")
                note_ids.add(note.id)
                if not note.text.strip() or len(note.text) > NOTE_LENGTH_LIMIT:
                    raise ValueError(f"note {note.id} is empty or too long")
                if not math.isfinite(note.written_at):
                    raise ValueError(f"note {note.id} has no time it was written")
                lasting = note.expires_at is None
                if lasting != (term == "long_term"):
                    raise ValueError(f"note {note.id} expiry does not match its term {term}")
                if note.expires_at is not None and not (
                    math.isfinite(note.expires_at) and note.expires_at > note.written_at
                ):
                    raise ValueError(f"note {note.id} never expires or expires before written")
            object.__setattr__(self, term, tuple(sorted(notes, key=lambda note: note.id)))


ActivityKind = Literal["started_walking", "moved", "started_mining", "finished_mining", "left_game"]


@dataclass(frozen=True)
class Activity:
    """Something a resident did at a place on the map, which others nearby can notice."""

    seq: int
    """Unique within a world, counting up from 1 in the order things happened."""
    time: float
    resident_id: str
    kind: ActivityKind
    position: Position
    """Where it happened; for a walk setting off, where it heads; for a move, where the
    resident got to: the end of the walk, or where it stopped."""
    origin: Position | None = None
    """Where a walk or move started; None for every other kind."""
    pool_id: str | None = None
    """The pool mined, for the two mining kinds; None otherwise."""


ACTIVITY_LIMIT = 500
"""Entries the activity log keeps; older ones are dropped, so the log stays small."""


@dataclass(frozen=True)
class ActivityLog:
    """Recent activity, oldest first, and the sequence number the next entry will get."""

    entries: tuple[Activity, ...] = ()
    next_seq: int = 1

    def __post_init__(self) -> None:
        if type(self.next_seq) is not int or self.next_seq < 1:
            raise ValueError(f"next activity seq must be an integer >= 1, got {self.next_seq!r}")
        if len(self.entries) > ACTIVITY_LIMIT:
            raise ValueError(f"activity log holds {len(self.entries)} entries, over the limit")
        previous = 0
        for entry in self.entries:
            if type(entry.seq) is not int or not previous < entry.seq < self.next_seq:
                raise ValueError(
                    f"activity seq {entry.seq!r} must be an integer above {previous} "
                    f"and below {self.next_seq}"
                )
            previous = entry.seq


@dataclass(frozen=True)
class Resident:
    id: str
    name: str
    color: str
    """Display colour as `#rrggbb`."""
    position: Position
    inventory: Inventory = Inventory()
    coins: int = 0
    """Spendable coins; those set aside by pending offers are not included, see `trade.py`."""
    left: bool = False
    """Left the game: stays in the world and the ranking, but can no longer act or trade."""
    mining: MiningJob | None = None
    walking: WalkJob | None = None
    """On the way somewhere: `position` is where they set off, see `walking.position_at`."""
    memory: Memory = Memory()

    def __post_init__(self) -> None:
        if not COLOR_PATTERN.fullmatch(self.color):
            raise ValueError(f"resident {self.id} has an invalid color: {self.color!r}")
        # Exactly int, as for item counts.
        if type(self.coins) is not int or self.coins < 0:
            raise ValueError(
                f"resident {self.id} has {self.coins!r} coins, must be an integer >= 0"
            )
        if self.mining is not None:
            if self.left:
                raise ValueError(f"resident {self.id} left the game but is still mining")
            if type(self.mining.amount) is not int or self.mining.amount < 1:
                raise ValueError(f"resident {self.id} is mining {self.mining.amount!r} tokens")
            if not math.isfinite(self.mining.ends_at):
                raise ValueError(f"resident {self.id} has a mining job that never ends")
            if self.inventory.count(self.mining.chip) < 1:
                raise ValueError(f"resident {self.id} is mining without carrying its chip")
        if self.walking is not None:
            if self.left:
                raise ValueError(f"resident {self.id} left the game but is still walking")
            if self.mining is not None:
                raise ValueError(f"resident {self.id} is walking and mining at once")
            walk = self.walking
            if not (
                math.isfinite(walk.departs_at)
                and math.isfinite(walk.arrives_at)
                and walk.departs_at <= walk.arrives_at
            ):
                raise ValueError(f"resident {self.id} has a walk that never ends or ends early")


@dataclass(frozen=True)
class Pool:
    """A token mine on the ground, see `mining.py`. Pools are placed when the game starts and
    never move or appear later; only their reserve goes down."""

    id: str
    position: Position
    radius: float
    """Residents within this distance of `position` can mine here."""
    reserve: int
    """Tokens still in the ground, not counting those miners are digging out right now."""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.radius) and self.radius > 0):
            raise ValueError(f"pool {self.id} has radius {self.radius}, must be positive")
        if type(self.reserve) is not int or self.reserve < 0:
            raise ValueError(f"pool {self.id} holds {self.reserve!r} tokens, must be >= 0")

    def reaches(self, position: Position) -> bool:
        return (
            math.dist((self.position.x, self.position.y), (position.x, position.y)) <= self.radius
        )


@dataclass(frozen=True)
class Market:
    """Where residents sell tokens for coins, see `market.py`. It never moves; only the
    pressure of recent sales, which sets the price, changes."""

    position: Position
    radius: float
    """Residents within this distance of `position` can sell here."""
    pressure: float = 0.0
    """Tokens sold recently, as measured at `pressure_at`; it fades as time passes."""
    pressure_at: float = 0.0
    """Game time of the last sale, see `World.time`."""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.radius) and self.radius > 0):
            raise ValueError(f"market has radius {self.radius}, must be positive")
        if not (math.isfinite(self.pressure) and self.pressure >= 0):
            raise ValueError(f"market pressure must be a finite number >= 0, got {self.pressure}")
        if not (math.isfinite(self.pressure_at) and self.pressure_at >= 0):
            raise ValueError(f"market pressure time must be finite and >= 0: {self.pressure_at}")

    def reaches(self, position: Position) -> bool:
        return (
            math.dist((self.position.x, self.position.y), (position.x, position.y)) <= self.radius
        )


@dataclass(frozen=True)
class WorldMap:
    """Side length of the square map, in map units."""

    size: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.size) and self.size > 0):
            raise ValueError(f"map size must be a positive finite number, got {self.size}")

    def contains(self, position: Position) -> bool:
        # NaN fails every comparison, so it is never inside the map.
        return 0 <= position.x <= self.size and 0 <= position.y <= self.size


@dataclass(frozen=True)
class World:
    map: WorldMap
    residents: tuple[Resident, ...]
    events: EventQueue = EventQueue()
    """Requests between residents waiting for an answer, see `events.py`."""
    ended: bool = False
    """The game is over: nothing changes any more, see `lifecycle.py`."""
    pools: tuple[Pool, ...] = ()
    """Token pools, fixed for the whole game."""
    time: float = 0.0
    """Seconds since the game started, as of the latest change; the server moves it on."""
    activity: ActivityLog = ActivityLog()
    """What residents have done where, for others nearby to notice, see `perception.py`."""
    market: Market | None = None
    """Where tokens sell for coins, see `market.py`; None on a map without one."""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.time) and self.time >= 0):
            raise ValueError(f"world time must be a finite number >= 0, got {self.time}")
        if self.market is not None:
            if not self.map.contains(self.market.position):
                raise ValueError(f"the market is outside the map: {self.market.position}")
            if self.market.pressure_at > self.time:
                raise ValueError("the market was last sold to after the current time")
        pool_ids = [pool.id for pool in self.pools]
        if len(set(pool_ids)) != len(pool_ids):
            raise ValueError("pool ids must be unique")
        for pool in self.pools:
            if not self.map.contains(pool.position):
                raise ValueError(f"pool {pool.id} is outside the map: {pool.position}")
        ids = [resident.id for resident in self.residents]
        if len(set(ids)) != len(ids):
            raise ValueError("resident ids must be unique")
        for resident in self.residents:
            if not self.map.contains(resident.position):
                raise ValueError(f"resident {resident.id} is outside the map: {resident.position}")
            if resident.mining is not None:
                if resident.mining.pool_id not in pool_ids:
                    raise ValueError(f"resident {resident.id} mines unknown pool")
                if self.ended:
                    raise ValueError(f"resident {resident.id} is mining after the game ended")
            if resident.walking is not None:
                if not self.map.contains(resident.walking.target):
                    raise ValueError(f"resident {resident.id} walks to outside the map")
                if self.ended:
                    raise ValueError(f"resident {resident.id} is walking after the game ended")
            for sighting in resident.memory.known_pools:
                if sighting.pool_id not in pool_ids:
                    raise ValueError(f"resident {resident.id} remembers unknown pool")
            if resident.memory.activity_seen >= self.activity.next_seq:
                raise ValueError(f"resident {resident.id} has seen activity not yet logged")
        for entry in self.activity.entries:
            if entry.resident_id not in ids:
                raise ValueError(f"activity {entry.seq} involves unknown resident")
            if entry.pool_id is not None and entry.pool_id not in pool_ids:
                raise ValueError(f"activity {entry.seq} involves unknown pool")
        for event in self.events.pending:
            for party in (event.sender_id, event.recipient_id):
                if party not in ids:
                    raise ValueError(f"event {event.id} involves unknown resident {party!r}")
            if event.sender_id == event.recipient_id:
                raise ValueError(f"event {event.id} is addressed to its own sender")
        # Leaving and ending both settle every pending event first.
        left = {resident.id for resident in self.residents if resident.left}
        for event in self.events.pending:
            if self.ended or {event.sender_id, event.recipient_id} & left:
                raise ValueError(f"event {event.id} is pending after its game or resident ended")
