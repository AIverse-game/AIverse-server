"""Buying a parcel together: two residents put their coins up for one parcel.

A resident standing in a parcel nobody owns proposes to another, with a `JointPurchase`, an
event, see `events.py`: the partner pays `partner_pays` coins towards the price and gets
`partner_shares` of the parcel's shares, see `shares.py`; the proposer pays the rest and keeps
the other shares. Sending sets aside the proposer's part, so it cannot be spent twice. When the
partner accepts and pays theirs, both pay the town and the parcel is theirs: the proposer is
its buyer, who alone builds on it, as when buying alone, see
`land.py`. The partner need not be there. Rejecting or withdrawing gives the proposer's part
back. If someone buys the parcel first, accepting fails and the offer stays pending, so it can
still be rejected or withdrawn.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.events import EventPayload
from ..core.inventory import add_coins, take_coins
from ..core.land import PARCEL_SHARES
from ..core.ledger import SYSTEM, record_transfer
from .land import add_block, check_for_sale, plot
from .shares import MAX_PENDING_PER_PAIR, SharesError

if TYPE_CHECKING:
    from ..core.events import Event
    from ..core.world import World


def reserved_purchase_coins(world: World, resident_id: str) -> int:
    """Coins the resident has set aside in joint purchases they proposed that still wait for an
    answer."""
    return sum(
        event.payload.proposer_pays(world)
        for event in world.events.pending
        if event.sender_id == resident_id and isinstance(event.payload, JointPurchase)
    )


@dataclass(frozen=True)
class JointPurchase(EventPayload):
    """The sender and the recipient buy the parcel at `col`, `row` together."""

    col: int
    row: int
    partner_pays: int
    """Coins the recipient pays of the price; the sender pays the rest."""
    partner_shares: int
    """Shares the recipient gets; the sender gets the other `PARCEL_SHARES` minus these."""

    def proposer_pays(self, world: World) -> int:
        return plot(world, self.col, self.row).price - self.partner_pays

    def on_send(self, world: World, event: Event) -> World:
        # Exactly int, as for share offers: True is not one share.
        if type(self.partner_shares) is not int or not 1 <= self.partner_shares < PARCEL_SHARES:
            raise SharesError(
                "invalid_shares",
                f"the partner's shares must be an integer in 1..{PARCEL_SHARES - 1}",
            )
        square = plot(world, self.col, self.row)
        if type(self.partner_pays) is not int or not 0 <= self.partner_pays <= square.price:
            raise SharesError(
                "invalid_shares", f"the partner pays an integer in 0..{square.price} coins"
            )
        check_for_sale(world, square)
        waiting = sum(
            other.sender_id == event.sender_id
            and other.recipient_id == event.recipient_id
            and isinstance(other.payload, JointPurchase)
            for other in world.events.pending
        )
        if waiting >= MAX_PENDING_PER_PAIR:
            raise SharesError(
                "too_many_offers",
                f"you already have {MAX_PENDING_PER_PAIR} joint purchases waiting for them",
            )
        own = self.proposer_pays(world)
        return take_coins(world, event.sender_id, own) if own else world

    def on_accept(self, world: World, event: Event) -> World:
        square = plot(world, self.col, self.row)
        check_for_sale(world, square)
        if self.partner_pays:
            world = take_coins(world, event.recipient_id, self.partner_pays)
            world = record_transfer(world, event.recipient_id, SYSTEM, self.partner_pays, "land")
        own = self.proposer_pays(world)
        if own:
            world = record_transfer(world, event.sender_id, SYSTEM, own, "land")
        shares = (
            (event.sender_id, PARCEL_SHARES - self.partner_shares),
            (event.recipient_id, self.partner_shares),
        )
        return add_block(world, self.col, self.row, event.sender_id, shares)

    def on_decline(self, world: World, event: Event) -> World:
        own = self.proposer_pays(world)
        return add_coins(world, event.sender_id, own) if own else world
