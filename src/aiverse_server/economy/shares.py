"""Parcel shares: who a facility's income is paid to, and how they change hands.

A parcel is cut into `PARCEL_SHARES` shares. Buying it gives the buyer all of them; whatever
the facility on it earns is paid out to the holders in proportion, `split_income`. A holder
may hand shares to another resident, for coins or for nothing, with a `ShareOffer`: an event,
see `events.py`, which the recipient accepts or rejects. Land is bought by the block, and so
shares change hands by the block too: an offer names any parcel of a block and moves the same
shares of all its parcels, so every parcel of a block always has the same holders. Several
residents holding a block together is how they share its facilities' income. A holder may have
at most `MAX_PENDING_PER_PAIR` offers waiting for the same recipient. Shares are only about money:
the buyer of the parcel stays the one who builds on it and puts content in it, see
`Parcel.owner_id`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from ..core.events import EventPayload
from ..core.inventory import add_coins, take_coins
from ..core.land import PARCEL_SHARES, Parcel
from ..core.ledger import record_transfer
from .land import block_parcels
from .trade import MAX_AMOUNT

if TYPE_CHECKING:
    from ..core.events import Event
    from ..core.world import World

MAX_PENDING_PER_PAIR = 3
"""Share offers one resident may have waiting for the same recipient. Offering costs nothing
at the time, so without a limit they could bury the recipient in offers."""

SharesErrorCode = Literal[
    "invalid_shares", "unknown_parcel", "not_enough_shares", "too_many_offers"
]


class SharesError(Exception):
    def __init__(self, code: SharesErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: SharesErrorCode = code
        self.message = message


def split_income(parcel: Parcel, income: int) -> tuple[tuple[str, int], ...]:
    """What each holder of `parcel` is paid out of `income` coins, holders owed nothing left
    out. Each gets their proportion rounded down; the coins left over go one by one to the
    holders with the largest fractions, the holder id settling a tie. The parts always add up
    to `income`."""
    holders = parcel.shares
    parts = [income * count // PARCEL_SHARES for _, count in holders]
    left = income - sum(parts)
    # The leftover is less than the number of holders, since each fraction is under one coin.
    fractions = [income * count % PARCEL_SHARES for _, count in holders]
    by_fraction = sorted(range(len(holders)), key=lambda i: (-fractions[i], holders[i][0]))
    for index in by_fraction[:left]:
        parts[index] += 1
    return tuple((holder, part) for (holder, _), part in zip(holders, parts, strict=True) if part)


def reserved_shares(world: World, resident_id: str, parcel_id: str) -> int:
    """Shares of the block holding `parcel_id` the resident has promised in share offers still
    waiting for an answer. They stay the resident's, and keep earning for them, until an offer
    is accepted."""
    ids = {parcel.id for parcel in block_parcels(world, parcel_id)}
    return sum(
        event.payload.shares
        for event in world.events.pending
        if event.sender_id == resident_id
        and isinstance(event.payload, ShareOffer)
        and event.payload.parcel_id in ids
    )


@dataclass(frozen=True)
class ShareOffer(EventPayload):
    """The sender gives the recipient `shares` of the block holding the parcel `parcel_id`, in
    every parcel of it, for `price` coins. Nothing is moved before the recipient accepts; the
    shares the sender has promised cannot be promised again."""

    parcel_id: str
    shares: int
    price: int
    """Coins for all of them; 0 gives them away."""

    def on_send(self, world: World, event: Event) -> World:
        # Exactly int, as for trades: True is not one share, and a huge integer cannot be printed.
        if type(self.shares) is not int or not 1 <= self.shares <= PARCEL_SHARES:
            raise SharesError("invalid_shares", f"shares must be an integer in 1..{PARCEL_SHARES}")
        if type(self.price) is not int or not 0 <= self.price <= MAX_AMOUNT:
            raise SharesError("invalid_shares", f"price must be an integer in 0..{MAX_AMOUNT}")
        parcel = _find(world, self.parcel_id)[0]
        waiting = sum(
            other.sender_id == event.sender_id
            and other.recipient_id == event.recipient_id
            and isinstance(other.payload, ShareOffer)
            for other in world.events.pending
        )
        if waiting >= MAX_PENDING_PER_PAIR:
            raise SharesError(
                "too_many_offers",
                f"you already have {MAX_PENDING_PER_PAIR} share offers waiting for them",
            )
        free = parcel.held_by(event.sender_id) - reserved_shares(
            world, event.sender_id, self.parcel_id
        )
        if self.shares > free:
            raise SharesError(
                "not_enough_shares",
                f"{event.sender_id} holds {free} shares of {self.parcel_id} to offer",
            )
        return world

    def on_accept(self, world: World, event: Event) -> World:
        block = _find(world, self.parcel_id)
        if min(parcel.held_by(event.sender_id) for parcel in block) < self.shares:
            raise SharesError(
                "not_enough_shares", f"{event.sender_id} no longer holds that many shares"
            )
        if self.price:
            world = take_coins(world, event.recipient_id, self.price)
            world = add_coins(world, event.sender_id, self.price)
            world = record_transfer(
                world, event.recipient_id, event.sender_id, self.price, "shares"
            )
        moved = {}
        for parcel in block:
            counts = dict(parcel.shares)
            counts[event.sender_id] -= self.shares
            counts[event.recipient_id] = counts.get(event.recipient_id, 0) + self.shares
            moved[parcel.id] = replace(
                parcel, shares=tuple((holder, count) for holder, count in counts.items() if count)
            )
        parcels = tuple(moved.get(p.id, p) for p in world.parcels)
        return replace(world, parcels=parcels)


def _find(world: World, parcel_id: str) -> tuple[Parcel, ...]:
    block = block_parcels(world, parcel_id)
    if not block:
        raise SharesError("unknown_parcel", f"no one owns a parcel {parcel_id!r}")
    return block
