"""Authoritative world state: a finite square map with a continuous coordinate system.

Coordinates: the origin (0, 0) is the north-west (top-left) corner, x grows east
(right) and y grows south (down), matching screen space. Both axes cover the closed
range [0, size]. Positions are real numbers, not grid cells: residents and later other
places can stand anywhere inside the map.
"""

import math
import re
from dataclasses import dataclass
from typing import Literal

from .compute import ComputeBook
from .events import EventQueue
from .land import Link, Parcel
from .ledger import Ledger
from .orders import OrderBook

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
class WalkJob:
    """A resident on their way to `target`, see `walking.py`; they can do nothing else until
    they arrive. Their `Resident.position` stays where they set off until then."""

    target: Position
    departs_at: float
    arrives_at: float
    """Game times when the walk began and when it ends, see `World.time`."""


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
    """What a resident has looked at so far, see `perception.py`, and the notes they chose to
    keep, see `memory.py`. Empty when the game starts."""

    activity_seen: int = 0
    """Sequence number of the last activity entry already looked at; 0 before the first look."""
    long_term: tuple[Note, ...] = ()
    """Notes kept for the rest of the game, oldest first."""
    short_term: tuple[Note, ...] = ()
    """Notes cleared once they expire, oldest first; expired ones may linger until the memory
    is next used, see `memory.forget_expired`."""
    next_note_id: int = 1
    """Id the next note gets; it only counts up, so ids are never reused within a game."""
    speech_seen: int = 0
    """Sequence number of the last line of speech already looked at; 0 before the first look."""
    notices_seen: int = 0
    """Sequence number of the last server notice already looked at; 0 before the first look."""

    def __post_init__(self) -> None:
        if type(self.activity_seen) is not int or self.activity_seen < 0:
            raise ValueError(f"activity seen must be an integer >= 0, got {self.activity_seen!r}")
        if type(self.speech_seen) is not int or self.speech_seen < 0:
            raise ValueError(f"speech seen must be an integer >= 0, got {self.speech_seen!r}")
        if type(self.notices_seen) is not int or self.notices_seen < 0:
            raise ValueError(f"notices seen must be an integer >= 0, got {self.notices_seen!r}")
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


ActivityKind = Literal["started_walking", "moved", "left_game"]


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


SPEECH_LENGTH_LIMIT = 100
"""Characters one line of speech may hold, as the design document's `say`."""

SPEECH_LIMIT = 200
"""Lines of speech the world keeps; older ones are dropped, so the record stays small."""


@dataclass(frozen=True)
class Speech:
    """Something a resident said, aloud or to one resident, see `speech.py`."""

    seq: int
    """Unique within a world, counting up from 1 in the order things were said."""
    time: float
    speaker_id: str
    text: str
    position: Position
    """Where the speaker stood, on the way if walking."""
    to: str | None
    """The one resident spoken to; None when spoken aloud."""
    listeners: tuple[str, ...]
    """Who heard it, not counting the speaker: `to` alone, or everyone in earshot then."""


@dataclass(frozen=True)
class SpeechLog:
    """Recent speech, oldest first, and the sequence number the next line will get."""

    entries: tuple[Speech, ...] = ()
    next_seq: int = 1

    def __post_init__(self) -> None:
        if type(self.next_seq) is not int or self.next_seq < 1:
            raise ValueError(f"next speech seq must be an integer >= 1, got {self.next_seq!r}")
        if len(self.entries) > SPEECH_LIMIT:
            raise ValueError(f"speech log holds {len(self.entries)} lines, over the limit")
        previous = 0
        for line in self.entries:
            if type(line.seq) is not int or not previous < line.seq < self.next_seq:
                raise ValueError(
                    f"speech seq {line.seq!r} must be an integer above {previous} "
                    f"and below {self.next_seq}"
                )
            previous = line.seq
            if not line.text.strip() or len(line.text) > SPEECH_LENGTH_LIMIT:
                raise ValueError(f"speech {line.seq} is empty or too long")
            # Every state carries speech out, so a line that cannot be encoded would stop them all.
            try:
                line.text.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(f"speech {line.seq} cannot be encoded as UTF-8") from None
            if line.speaker_id in line.listeners or len(set(line.listeners)) != len(line.listeners):
                raise ValueError(f"speech {line.seq} lists its speaker or a listener twice")
            if line.to is not None and line.listeners != (line.to,):
                raise ValueError(f"speech {line.seq} to one resident was heard by others")


@dataclass(frozen=True)
class Notice:
    """An announcement the server makes to every resident, see `lifecycle.announce`."""

    seq: int
    """Unique within a world, counting up from 1 in the order things were announced."""
    time: float
    text: str


@dataclass(frozen=True)
class NoticeLog:
    """The notices announced so far, oldest first, and the sequence number the next gets."""

    entries: tuple[Notice, ...] = ()
    next_seq: int = 1

    def __post_init__(self) -> None:
        if type(self.next_seq) is not int or self.next_seq < 1:
            raise ValueError(f"next notice seq must be an integer >= 1, got {self.next_seq!r}")
        previous = 0
        for notice in self.entries:
            if type(notice.seq) is not int or not previous < notice.seq < self.next_seq:
                raise ValueError(
                    f"notice seq {notice.seq!r} must be an integer above {previous} "
                    f"and below {self.next_seq}"
                )
            previous = notice.seq
            if not notice.text.strip():
                raise ValueError(f"notice {notice.seq} is empty")


@dataclass(frozen=True)
class Contract:
    """A standing deal between two residents, see `contracts.py`: the supplier is to deliver
    `quantity` of `kind` to the buyer `deliveries_left` more times by `expires_at`, and each
    delivery pays them `price` coins and returns `deposit` of their own."""

    id: int
    """Unique within a world and never reused, counting up from 1."""
    supplier_id: str
    buyer_id: str
    kind: str
    quantity: int
    price: int
    """Coins for one delivery."""
    deliveries_left: int
    """The buyer's coins for all of them wait set aside, `price` times this many."""
    signed_at: float
    expires_at: float
    """Game times when the contract was agreed and when it lapses, see `World.time`."""
    deposit: int = 0
    """Coins the supplier stakes on one delivery, set aside with the buyer's; 0 for none."""

    @property
    def escrow(self) -> int:
        """Coins set aside from the buyer for the deliveries still to come."""
        return self.price * self.deliveries_left

    @property
    def deposit_held(self) -> int:
        """Coins set aside from the supplier for the deliveries still to come."""
        return self.deposit * self.deliveries_left


ContractEndReason = Literal["completed", "lapsed", "cancelled", "party_left", "game_ended"]


@dataclass(frozen=True)
class ContractEnd:
    """How a contract ended, see `contracts.py`."""

    contract: Contract
    """The contract as it stood when it ended; `deliveries_left` are those never made."""
    time: float
    reason: ContractEndReason
    by: str | None
    """The party whose doing ended it: the supplier for the last delivery or a cancellation,
    the one who left; None when it lapsed or the game ended."""
    forfeit: int
    """The supplier's deposit paid to the buyer for the deliveries never made."""


CONTRACT_END_LIMIT = 100
"""Ended contracts the world keeps; older ones are dropped, so the record stays small."""


@dataclass(frozen=True)
class ContractBook:
    """Contracts in force, oldest first, the id the next one will get, and recently ended
    contracts in the order they ended."""

    active: tuple[Contract, ...] = ()
    next_id: int = 1
    ended: tuple[ContractEnd, ...] = ()

    def __post_init__(self) -> None:
        if type(self.next_id) is not int or self.next_id < 1:
            raise ValueError(f"next contract id must be an integer >= 1, got {self.next_id!r}")
        previous = 0
        for contract in self.active:
            if type(contract.id) is not int or not previous < contract.id < self.next_id:
                raise ValueError(
                    f"contract id {contract.id!r} must be an integer above {previous} "
                    f"and below {self.next_id}"
                )
            previous = contract.id
            _check_contract(contract, least_left=1)
        if len(self.ended) > CONTRACT_END_LIMIT:
            raise ValueError(f"{len(self.ended)} ended contracts kept, over the limit")
        ids = [contract.id for contract in self.active]
        last = -math.inf
        for end in self.ended:
            contract = end.contract
            if type(contract.id) is not int or not 1 <= contract.id < self.next_id:
                raise ValueError(f"ended contract id {contract.id!r} was never given out")
            ids.append(contract.id)
            # Only the last delivery leaves none to make.
            _check_contract(contract, least_left=0 if end.reason == "completed" else 1)
            if end.reason == "completed" and contract.deliveries_left != 0:
                raise ValueError(f"contract {contract.id} completed with deliveries left")
            if not (math.isfinite(end.time) and max(contract.signed_at, last) <= end.time):
                raise ValueError(f"contract {contract.id} ended before signed or out of order")
            last = end.time
            if not 0 <= end.forfeit <= contract.deposit_held:
                raise ValueError(f"contract {contract.id} forfeited more than its deposit")
        if len(set(ids)) != len(ids):
            raise ValueError("a contract is recorded twice")


def _check_contract(contract: Contract, least_left: int) -> None:
    for amount, least in (
        (contract.quantity, 1),
        (contract.price, 1),
        (contract.deliveries_left, least_left),
        (contract.deposit, 0),
    ):
        if type(amount) is not int or amount < least:
            raise ValueError(f"contract {contract.id} has {amount!r}, must be >= {least}")
    if not contract.kind:
        raise ValueError(f"contract {contract.id} has no item kind")
    if not (
        math.isfinite(contract.signed_at)
        and math.isfinite(contract.expires_at)
        and contract.signed_at < contract.expires_at
    ):
        raise ValueError(f"contract {contract.id} never lapses or lapses before signed")


@dataclass(frozen=True)
class Resident:
    id: str
    name: str
    color: str
    """Display colour as `#rrggbb`."""
    position: Position
    inventory: Inventory = Inventory()
    coins: int = 0
    """Spendable coins; those set aside by pending offers and contracts are not included, see
    `trade.py` and `contracts.py`."""
    left: bool = False
    """Left the game: stays in the world and the ranking, but can no longer act or trade."""
    walking: WalkJob | None = None
    """On the way somewhere: `position` is where they set off, see `walking.position_at`."""
    memory: Memory = Memory()

    @property
    def busy(self) -> bool:
        """In the middle of a walk: they can do nothing but talk or leave."""
        return self.walking is not None

    def __post_init__(self) -> None:
        if not COLOR_PATTERN.fullmatch(self.color):
            raise ValueError(f"resident {self.id} has an invalid color: {self.color!r}")
        # Exactly int, as for item counts.
        if type(self.coins) is not int or self.coins < 0:
            raise ValueError(
                f"resident {self.id} has {self.coins!r} coins, must be an integer >= 0"
            )
        if self.walking is not None:
            if self.left:
                raise ValueError(f"resident {self.id} left the game but is still walking")
            walk = self.walking
            if not (
                math.isfinite(walk.departs_at)
                and math.isfinite(walk.arrives_at)
                and walk.departs_at <= walk.arrives_at
            ):
                raise ValueError(f"resident {self.id} has a walk that never ends or ends early")


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
    time: float = 0.0
    """Seconds since the game started, as of the latest change; the server moves it on."""
    activity: ActivityLog = ActivityLog()
    """What residents have done where, for others nearby to notice, see `perception.py`."""
    speech: SpeechLog = SpeechLog()
    """What residents have said and who heard it, see `speech.py`."""
    notices: NoticeLog = NoticeLog()
    """What the server has announced to every resident, see `lifecycle.announce`."""
    contracts: ContractBook = ContractBook()
    """Standing deals between residents, see `contracts.py`."""
    ledger: Ledger = Ledger()
    """Every coin that changed hands, see `ledger.py`."""
    orders: OrderBook = OrderBook()
    """The exchange's resting orders and recent fills, see `exchange.py`."""
    parcels: tuple[Parcel, ...] = ()
    """Land residents bought and what they built on it, see `land.py` and `compute.py`."""
    compute: ComputeBook = ComputeBook()
    """Subscribed nodes and running jobs, see `compute.py`."""
    links: tuple[Link, ...] = ()
    """Lines between portals, see `portals.py`."""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.time) and self.time >= 0):
            raise ValueError(f"world time must be a finite number >= 0, got {self.time}")
        ids = [resident.id for resident in self.residents]
        if len(set(ids)) != len(ids):
            raise ValueError("resident ids must be unique")
        for resident in self.residents:
            if not self.map.contains(resident.position):
                raise ValueError(f"resident {resident.id} is outside the map: {resident.position}")
            if resident.walking is not None:
                if not self.map.contains(resident.walking.target):
                    raise ValueError(f"resident {resident.id} walks to outside the map")
                if self.ended:
                    raise ValueError(f"resident {resident.id} is walking after the game ended")
            if resident.memory.activity_seen >= self.activity.next_seq:
                raise ValueError(f"resident {resident.id} has seen activity not yet logged")
            if resident.memory.speech_seen >= self.speech.next_seq:
                raise ValueError(f"resident {resident.id} has heard speech not yet said")
            if resident.memory.notices_seen >= self.notices.next_seq:
                raise ValueError(f"resident {resident.id} has read a notice not yet announced")
        for entry in self.activity.entries:
            if entry.resident_id not in ids:
                raise ValueError(f"activity {entry.seq} involves unknown resident")
        for line in self.speech.entries:
            if {line.speaker_id, *line.listeners} - set(ids):
                raise ValueError(f"speech {line.seq} involves unknown resident")
        for event in self.events.pending:
            for party in (event.sender_id, event.recipient_id):
                if party not in ids:
                    raise ValueError(f"event {event.id} involves unknown resident {party!r}")
            if event.sender_id == event.recipient_id:
                raise ValueError(f"event {event.id} is addressed to its own sender")
        # Leaving and ending both settle every pending event first.
        left = {resident.id for resident in self.residents if resident.left}
        parcel_ids = [parcel.id for parcel in self.parcels]
        if len(set(parcel_ids)) != len(parcel_ids):
            raise ValueError("a parcel is owned twice")
        for parcel in self.parcels:
            if parcel.owner_id not in ids:
                raise ValueError(f"parcel {parcel.id} has an unknown owner")
            if any(holder not in ids for holder, _ in parcel.shares):
                raise ValueError(f"parcel {parcel.id} has an unknown share holder")
            if parcel.facility is not None and parcel.facility.built_at > self.time:
                raise ValueError(f"parcel {parcel.id} has a facility built in the future")
        owners = {parcel.id: parcel.owner_id for parcel in self.parcels}
        portals = {
            parcel.id
            for parcel in self.parcels
            if parcel.facility and parcel.facility.kind == "portal"
        }
        pairs = [(link.a, link.b) for link in self.links]
        if len(set(pairs)) != len(pairs):
            raise ValueError("two portals are linked twice")
        for link in self.links:
            if not {link.a, link.b} <= portals:
                raise ValueError(f"link {link.a}-{link.b} joins a parcel without a portal")
        for node in self.compute.nodes:
            if node.owner_id not in ids or node.owner_id in left or self.ended:
                raise ValueError(f"node {node.id} outlives its game or resident")
            if node.bought_at > self.time:
                raise ValueError(f"node {node.id} was bought after the current time")
        for job in self.compute.jobs:
            if job.owner_id not in ids or job.owner_id in left or self.ended:
                raise ValueError(f"job {job.id} outlives its game or resident")
            if job.parcel_id is not None and owners.get(job.parcel_id) != job.owner_id:
                raise ValueError(f"job {job.id} works on a parcel that is not its owner's")
            if job.started_at > self.time:
                raise ValueError(f"job {job.id} started after the current time")
            if any(p.id == job.parcel_id and p.facility is not None for p in self.parcels):
                raise ValueError(f"job {job.id} builds on a parcel that already has a facility")
        for order in self.orders.orders:
            if order.resident_id not in ids or order.resident_id in left or self.ended:
                raise ValueError(f"order {order.id} outlives its game or resident")
        for fill in self.orders.fills:
            if {fill.buyer_id, fill.seller_id, fill.taker_id} - set(ids):
                raise ValueError(f"fill {fill.seq} involves unknown resident")
        for event in self.events.pending:
            if self.ended or {event.sender_id, event.recipient_id} & left:
                raise ValueError(f"event {event.id} is pending after its game or resident ended")
        # Likewise every contract ends first.
        for contract in self.contracts.active:
            parties = {contract.supplier_id, contract.buyer_id}
            if parties - set(ids) or len(parties) != 2:
                raise ValueError(f"contract {contract.id} needs two known residents")
            if self.ended or parties & left:
                raise ValueError(f"contract {contract.id} outlives its game or a party")
        for end in self.contracts.ended:
            parties = {end.contract.supplier_id, end.contract.buyer_id}
            if parties - set(ids) or len(parties) != 2 or end.by not in (*parties, None):
                raise ValueError(f"ended contract {end.contract.id} needs two known residents")
