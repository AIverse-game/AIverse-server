"""Building on someone else's parcel: a commission, which the other side must agree to.

Only the buyer of a parcel builds on it, but they need not do the work themselves. Either side
may start a `BuildCommission`, an event, see `events.py`: the owner asks a builder to build an
ad slot, a game hall, a workspace or a mall on their parcel for a fee, or the builder asks the
owner to let them build one there for a fee. The other side answers. Nothing is built, and no permit
exists, before it is accepted.

The owner's fee is set aside, at once when the owner sent the commission and on acceptance when
the builder did, so it counts towards the owner's ranking but cannot be spent twice. Accepting
gives the builder a `Permit`, see `core/compute.py`: for `PERMIT_SECONDS` they may start the
job with `start_build_ad` or `start_build_venue` on their own compute or nodes, on that parcel
only. The builder gets the fee when the facility is finished, `finish_jobs`. If the permit
lapses unused, or the job is dropped because the builder left or the game ended, the fee goes
back to the owner. A mall can run two ways, `Orientation`, and the owner has the say: they name
it when they send the commission, or, when the builder sent it, they may name it when they
accept, see `answer_commission`; the permit carries it and the builder builds as it says. The
facility belongs to the parcel, so its income goes to the holders of
the parcel's shares as for any facility; whoever wants the builder to share in it can
`offer_shares`. Putting content in the facility is still the owner's.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..core.compute import PERMIT_KINDS, Permit, PermitKind
from ..core.events import EventPayload
from ..core.inventory import add_coins, take_coins
from ..core.land import ORIENTATIONS, Orientation
from .compute import PERMIT_SECONDS, ComputeError, check_buildable, permit_coins
from .shares import MAX_PENDING_PER_PAIR

if TYPE_CHECKING:
    from ..core.events import Event
    from ..core.world import World

MAX_FEE = 100_000
"""Most coins a commission may pay for one facility."""


def reserved_commission_coins(world: World, resident_id: str) -> int:
    """Coins the resident has set aside in commissions they sent as the owner that still wait for
    an answer."""
    return sum(
        event.payload.fee
        for event in world.events.pending
        if event.sender_id == resident_id
        and isinstance(event.payload, BuildCommission)
        and event.payload.owner_id == resident_id
    )


def commission_coins(world: World, resident_id: str) -> int:
    """All the coins set aside for building on the resident's parcels: in commissions waiting for
    an answer, in permits not yet used and in jobs not yet finished."""
    return reserved_commission_coins(world, resident_id) + permit_coins(world, resident_id)


@dataclass(frozen=True)
class BuildCommission(EventPayload):
    """`builder_id` builds a facility of `kind` on the parcel `parcel_id` of `owner_id` for
    `fee` coins, paid by the owner when it is finished. The sender is one of the two and the
    recipient the other."""

    parcel_id: str
    kind: PermitKind
    owner_id: str
    builder_id: str
    fee: int
    orientation: Orientation = "north_south"
    """Which way a mall runs; the owner's choice, or the builder's suggestion until the owner
    accepts. The default for any other kind."""

    def on_send(self, world: World, event: Event) -> World:
        # Exactly int, as for share offers: True is not one coin.
        if type(self.fee) is not int or not 0 <= self.fee <= MAX_FEE:
            raise ComputeError("invalid_fee", f"the fee is a whole number of coins, 0 to {MAX_FEE}")
        if self.kind not in PERMIT_KINDS:
            raise ComputeError("invalid_commission", f"no facility of kind {self.kind!r} to build")
        if self.orientation not in ORIENTATIONS:
            raise ComputeError("invalid_orientation", f"no orientation {self.orientation!r}")
        if self.kind != "mall" and self.orientation != "north_south":
            raise ComputeError("invalid_orientation", f"a {self.kind} has no orientation")
        if {self.owner_id, self.builder_id} != {event.sender_id, event.recipient_id}:
            raise ComputeError(
                "invalid_commission", "the owner and the builder must be you and the recipient"
            )
        self._check(world)
        waiting = sum(
            other.sender_id == event.sender_id
            and other.recipient_id == event.recipient_id
            and isinstance(other.payload, BuildCommission)
            for other in world.events.pending
        )
        if waiting >= MAX_PENDING_PER_PAIR:
            raise ComputeError(
                "too_many_commissions",
                f"you already have {MAX_PENDING_PER_PAIR} build commissions waiting for them",
            )
        if event.sender_id == self.owner_id and self.fee:
            return take_coins(world, self.owner_id, self.fee)
        return world

    def on_accept(self, world: World, event: Event) -> World:
        self._check(world)
        if event.sender_id == self.builder_id and self.fee:
            world = take_coins(world, self.owner_id, self.fee)
        permit = Permit(
            self.parcel_id,
            self.kind,
            self.owner_id,
            self.builder_id,
            self.fee,
            world.time + PERMIT_SECONDS,
            self.orientation,
        )
        book = world.compute
        return replace(world, compute=replace(book, permits=(*book.permits, permit)))

    def on_decline(self, world: World, event: Event) -> World:
        if event.sender_id == self.owner_id and self.fee:
            return add_coins(world, self.owner_id, self.fee)
        return world

    def _check(self, world: World) -> None:
        """Refuse unless the parcel is the owner's and free to build on, and not promised."""
        parcel = next((p for p in world.parcels if p.id == self.parcel_id), None)
        if parcel is None:
            raise ComputeError("unknown_parcel", f"no one owns a parcel {self.parcel_id!r}")
        if parcel.owner_id != self.owner_id and parcel.held_by(self.owner_id) == 0:
            raise ComputeError(
                "not_your_parcel", f"parcel {self.parcel_id!r} is not {self.owner_id}'s"
            )
        check_buildable(world, self.owner_id, self.parcel_id, self.kind, self.orientation)


def answer_commission(world: World, event: Event, orientation: Orientation) -> World:
    """Let the parcel's owner, the recipient of the commission `event`, name the way the mall runs
    before they accept: the same world with the event's orientation replaced. Refused for a
    commission that is not for a mall, or when the recipient is the builder, whose owner has
    already said."""
    payload = event.payload
    if not isinstance(payload, BuildCommission) or payload.kind != "mall":
        raise ComputeError("invalid_orientation", "only a commission to build a mall has one")
    if event.recipient_id != payload.owner_id:
        raise ComputeError(
            "invalid_orientation", "the owner has already said which way the mall runs"
        )
    if orientation not in ORIENTATIONS:
        raise ComputeError("invalid_orientation", f"no orientation {orientation!r}")
    pending = tuple(
        replace(other, payload=replace(payload, orientation=orientation))
        if other.id == event.id
        else other
        for other in world.events.pending
    )
    return replace(world, events=replace(world.events, pending=pending))
