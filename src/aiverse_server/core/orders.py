"""Immutable exchange orders and fills stored in the world; rules live in economy/exchange.py."""

import math
from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]

FILL_LIMIT = 500
"""Fills the world keeps, the latest; older ones are dropped."""


@dataclass(frozen=True)
class Order:
    """A resting limit order. A buy order holds `quantity * price` coins set aside, a sell
    order holds `quantity` items; both shrink as the order fills."""

    id: int
    resident_id: str
    side: Side
    item: str
    quantity: int
    """Units still wanted or offered."""
    price: int
    """Coins per unit."""
    placed_at: float
    expires_at: float

    def __post_init__(self) -> None:
        for name, value in (("quantity", self.quantity), ("price", self.price)):
            if type(value) is not int or value < 1:
                raise ValueError(f"order {self.id} has {name} {value!r}, must be an integer >= 1")
        if not (math.isfinite(self.expires_at) and self.placed_at <= self.expires_at):
            raise ValueError(f"order {self.id} expires before it was placed")


@dataclass(frozen=True)
class Fill:
    """Units changing hands between two orders, at the resting order's price."""

    seq: int
    time: float
    item: str
    quantity: int
    price: int
    buyer_id: str
    seller_id: str
    taker_id: str
    """Whoever placed the incoming order; the other side's order was resting."""


@dataclass(frozen=True)
class OrderBook:
    orders: tuple[Order, ...] = ()
    """Resting orders of every item, oldest first."""
    next_id: int = 1
    fills: tuple[Fill, ...] = ()
    """The latest `FILL_LIMIT` fills, oldest first."""
    next_seq: int = 1

    def __post_init__(self) -> None:
        if type(self.next_id) is not int or self.next_id < 1:
            raise ValueError("invalid next order id")
        ids = [order.id for order in self.orders]
        if len(set(ids)) != len(ids) or any(not 1 <= id < self.next_id for id in ids):
            raise ValueError("order ids must be unique and already issued")
        if len(self.fills) > FILL_LIMIT:
            raise ValueError(f"an order book keeps at most {FILL_LIMIT} fills")
        if any(not 1 <= fill.seq < self.next_seq for fill in self.fills):
            raise ValueError("fill numbers must already be issued")


def order_coins(book: OrderBook, resident_id: str) -> int:
    """Coins the resident has set aside in resting buy orders."""
    return sum(
        order.quantity * order.price
        for order in book.orders
        if order.resident_id == resident_id and order.side == "buy"
    )


def order_items(book: OrderBook, resident_id: str, item: str) -> int:
    """Items of `item` the resident has set aside in resting sell orders."""
    return sum(
        order.quantity
        for order in book.orders
        if order.resident_id == resident_id and order.side == "sell" and order.item == item
    )
