"""Trades between two residents: items for coins, done once the other side agrees.

Either side may open the deal. A buyer sends a `PurchaseOffer` ("I will pay 10 coins for 3 of
your bread") to the seller, or a seller sends a `SaleOffer` ("I will sell you 3 bread for 10
coins") to the buyer. Both are events, see `events.py`: sending sets aside what the sender
promises, the buyer's coins or the seller's items, so it cannot be spent twice; accepting
takes the recipient's side and completes the swap; rejecting or withdrawing gives the sender
back what was set aside. If the recipient lacks their side when accepting, nothing changes
and the offer stays pending.

Coins are inventory items of kind `COIN` for now; whether they become a separate balance, and
where trading may happen on the map, are still open design questions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .events import EventPayload
from .inventory import store_item, take_item

if TYPE_CHECKING:
    from .events import Event
    from .world import World

COIN = "coin"

# A sanity bound, not a balance rule: keeps untrusted numbers small enough to print and count.
MAX_AMOUNT = 10**9

TradeErrorCode = Literal["invalid_trade"]


class TradeError(Exception):
    def __init__(self, code: TradeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: TradeErrorCode = code
        self.message = message


@dataclass(frozen=True)
class _Trade(EventPayload):
    kind: str
    """The item kind changing hands; never coins themselves."""
    quantity: int
    price: int
    """Coins for the whole lot, not per item."""

    def check(self) -> None:
        """Refuse terms that cannot make a trade. They come from untrusted requests, so this
        raises `TradeError` rather than `ValueError`."""
        if type(self.kind) is not str or not self.kind:
            raise TradeError("invalid_trade", "item kind must be a non-empty string")
        if self.kind == COIN:
            raise TradeError("invalid_trade", "coins cannot be traded for coins")
        # Exactly int: True is not one item, and floats such as NaN slip past `< 1`. The
        # messages leave the value out, since a huge integer cannot even be printed.
        if type(self.quantity) is not int or not 1 <= self.quantity <= MAX_AMOUNT:
            raise TradeError("invalid_trade", f"quantity must be an integer in 1..{MAX_AMOUNT}")
        # At least one coin: the design allows no free transfers between residents.
        if type(self.price) is not int or not 1 <= self.price <= MAX_AMOUNT:
            raise TradeError("invalid_trade", f"price must be an integer in 1..{MAX_AMOUNT} coins")

    def _swap(self, world: World, buyer_id: str, seller_id: str) -> World:
        world = store_item(world, buyer_id, self.kind, self.quantity)
        return store_item(world, seller_id, COIN, self.price)


@dataclass(frozen=True)
class PurchaseOffer(_Trade):
    """The sender buys from the recipient; the sender's coins wait set aside."""

    def on_send(self, world: World, event: Event) -> World:
        self.check()
        return take_item(world, event.sender_id, COIN, self.price)

    def on_accept(self, world: World, event: Event) -> World:
        world = take_item(world, event.recipient_id, self.kind, self.quantity)
        return self._swap(world, buyer_id=event.sender_id, seller_id=event.recipient_id)

    def on_decline(self, world: World, event: Event) -> World:
        return store_item(world, event.sender_id, COIN, self.price)


@dataclass(frozen=True)
class SaleOffer(_Trade):
    """The sender sells to the recipient; the sender's items wait set aside."""

    def on_send(self, world: World, event: Event) -> World:
        self.check()
        return take_item(world, event.sender_id, self.kind, self.quantity)

    def on_accept(self, world: World, event: Event) -> World:
        world = take_item(world, event.recipient_id, COIN, self.price)
        return self._swap(world, buyer_id=event.recipient_id, seller_id=event.sender_id)

    def on_decline(self, world: World, event: Event) -> World:
        return store_item(world, event.sender_id, self.kind, self.quantity)
