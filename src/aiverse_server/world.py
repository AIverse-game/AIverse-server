"""Authoritative world state: a finite square map with a continuous coordinate system.

Coordinates: the origin (0, 0) is the north-west (top-left) corner, x grows east
(right) and y grows south (down), matching screen space. Both axes cover the closed
range [0, size]. Positions are real numbers, not grid cells: residents, token pools and
later other places can stand anywhere inside the map.
"""

import math
import re
from dataclasses import dataclass

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

    def __post_init__(self) -> None:
        if not (math.isfinite(self.time) and self.time >= 0):
            raise ValueError(f"world time must be a finite number >= 0, got {self.time}")
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
