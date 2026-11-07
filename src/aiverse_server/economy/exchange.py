"""The exchange: an anonymous limit order book for every tradable item.

A resident anywhere on the map places an order to buy or sell an item at a unit price,
see `place_order`. A buy order sets aside its coins and a sell order its items, as offers
do, see `trade.py`. The new order first matches the best-priced orders resting on the other
side, oldest first at the same price, each fill at the resting order's price; a buyer who
offered more gets the difference back. What is left rests on the book until it fills, is
cancelled, expires, or its owner leaves or the game ends, all of which hand back what is still
set aside. Fills reach both sides wherever they are, and are logged without names for others
to read. The system never places orders.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import add_coins, store_item, take_coins, take_item
from ..core.ledger import record_transfer
from ..core.orders import FILL_LIMIT, Fill, Order, OrderBook, Side
from ..core.world import Resident, World

EXCHANGE_ITEMS: tuple[str, ...] = ()
"""Items with an order book. No item exists yet; the content residents make will be the first."""

MAX_ORDERS = 8
"""Resting orders one resident may have at a time; with that many, only an order that fills
at once in full is taken."""

MAX_QUANTITY = 10_000
MAX_PRICE = 1_000_000
"""Coins per unit an order may name, a bound that keeps totals small."""

LIFETIME_RANGE = (30.0, 600.0)
"""Least and most game seconds an order rests before it expires."""
DEFAULT_LIFETIME = 300.0

ExchangeErrorCode = Literal[
    "unknown_resident",
    "unknown_item",
    "invalid_order",
    "too_many_orders",
    "self_cross",
    "unknown_order",
]


class ExchangeError(Exception):
    def __init__(self, code: ExchangeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ExchangeErrorCode = code
        self.message = message


def place_order(
    world: World,
    resident_id: str,
    side: Side,
    item: str,
    quantity: int,
    price: int,
    lifetime: float = DEFAULT_LIFETIME,
) -> World:
    """Set aside what the order needs, match it against the other side and rest what is
    left; `InventoryError` when the resident carries too few coins or items."""
    _find(world, resident_id)
    if item not in EXCHANGE_ITEMS:
        raise ExchangeError("unknown_item", f"no order book for {item!r}")
    _check_terms(quantity, price, lifetime)
    own = [order for order in world.orders.orders if order.resident_id == resident_id]
    if any(
        order.item == item and order.side != side and _crosses(side, price, order.price)
        for order in own
    ):
        raise ExchangeError("self_cross", "the order would trade with your own resting order")
    if side == "buy":
        world = take_coins(world, resident_id, quantity * price)
    else:
        world = take_item(world, resident_id, item, quantity)
    book = world.orders
    order = Order(
        book.next_id, resident_id, side, item, quantity, price, world.time, world.time + lifetime
    )
    world = replace(world, orders=replace(book, next_id=book.next_id + 1))
    world = _match(world, order)
    # Only what is left to rest counts against the limit; refusing undoes the fills too.
    if len(own) >= MAX_ORDERS and any(o.id == order.id for o in world.orders.orders):
        raise ExchangeError("too_many_orders", f"at most {MAX_ORDERS} orders may rest at once")
    return world


def cancel_order(world: World, resident_id: str, order_id: int) -> World:
    """Take one of the resident's own orders off the book and hand back what it still holds."""
    _find(world, resident_id)
    order = next((order for order in world.orders.orders if order.id == order_id), None)
    if order is None or order.resident_id != resident_id:
        raise ExchangeError("unknown_order", f"resident {resident_id!r} has no order {order_id}")
    return _withdraw(world, order)


def expire_orders(world: World) -> World:
    """Take every order whose time is up by `world.time` off the book, handing back what it
    holds; the same world when none is."""
    for order in world.orders.orders:
        if order.expires_at <= world.time:
            world = _withdraw(world, order)
    return world


def next_order_expiry(world: World) -> float | None:
    """Game time when the earliest resting order expires, or None when none rests."""
    return min((order.expires_at for order in world.orders.orders), default=None)


def cancel_orders(world: World, resident_id: str | None = None) -> World:
    """Withdraw the resident's orders, or every order when `resident_id` is None."""
    for order in world.orders.orders:
        if resident_id is None or order.resident_id == resident_id:
            world = _withdraw(world, order)
    return world


def depth(world: World, item: str, side: Side, levels: int) -> tuple[tuple[int, int], ...]:
    """The best `levels` prices on one side of an item's book, each with the units resting at
    it, best first: highest for buy orders, lowest for sell orders."""
    totals: dict[int, int] = {}
    for order in world.orders.orders:
        if order.item == item and order.side == side:
            totals[order.price] = totals.get(order.price, 0) + order.quantity
    prices = sorted(totals, reverse=side == "buy")[:levels]
    return tuple((price, totals[price]) for price in prices)


def last_price(world: World, item: str) -> int | None:
    """Unit price of the item's latest fill the world still keeps, None without one."""
    return next((fill.price for fill in reversed(world.orders.fills) if fill.item == item), None)


def volume_since(world: World, item: str, since: float) -> int:
    """Units of the item that changed hands from game time `since` on."""
    return sum(f.quantity for f in world.orders.fills if f.item == item and f.time >= since)


def _check_terms(quantity: int, price: int, lifetime: float) -> None:
    low, high = LIFETIME_RANGE
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        raise ExchangeError(
            "invalid_order", f"quantity must be a whole number from 1 to {MAX_QUANTITY}"
        )
    if type(price) is not int or not 1 <= price <= MAX_PRICE:
        raise ExchangeError(
            "invalid_order", f"unit price must be a whole number from 1 to {MAX_PRICE}"
        )
    if not low <= lifetime <= high:
        raise ExchangeError("invalid_order", f"an order lasts {low:g} to {high:g} seconds")


def _crosses(side: Side, price: int, resting: int) -> bool:
    """Whether an order on `side` at `price` trades with an opposite order at `resting`."""
    return price >= resting if side == "buy" else price <= resting


def _match(world: World, incoming: Order) -> World:
    opposite = sorted(
        (
            order
            for order in world.orders.orders
            if order.item == incoming.item
            and order.side != incoming.side
            and _crosses(incoming.side, incoming.price, order.price)
        ),
        key=lambda order: (order.price if incoming.side == "buy" else -order.price, order.id),
    )
    left = incoming.quantity
    for resting in opposite:
        if left == 0:
            break
        quantity = min(left, resting.quantity)
        buyer, seller = (
            (incoming.resident_id, resting.resident_id)
            if incoming.side == "buy"
            else (resting.resident_id, incoming.resident_id)
        )
        world = _fill(world, incoming, resting, buyer, seller, quantity)
        left -= quantity
    if left == 0:
        return world
    book = world.orders
    return replace(
        world, orders=replace(book, orders=(*book.orders, replace(incoming, quantity=left)))
    )


def _fill(
    world: World, incoming: Order, resting: Order, buyer: str, seller: str, quantity: int
) -> World:
    """Hand over `quantity` units at the resting price and shrink or remove the resting order."""
    price = resting.price
    world = store_item(world, buyer, incoming.item, quantity)
    world = add_coins(world, seller, quantity * price)
    if incoming.side == "buy" and incoming.price > price:
        # The buyer set aside their own price; the difference comes back.
        world = add_coins(world, buyer, quantity * (incoming.price - price))
    world = record_transfer(
        world, buyer, seller, quantity * price, "exchange", incoming.item, quantity
    )
    book = world.orders
    left = resting.quantity - quantity
    orders = tuple(
        (replace(order, quantity=left) if left else None) if order.id == resting.id else order
        for order in book.orders
    )
    fill = Fill(
        book.next_seq,
        world.time,
        incoming.item,
        quantity,
        price,
        buyer,
        seller,
        incoming.resident_id,
    )
    fills = (*book.fills, fill)[-FILL_LIMIT:]
    return replace(
        world,
        orders=OrderBook(
            tuple(order for order in orders if order is not None),
            book.next_id,
            fills,
            book.next_seq + 1,
        ),
    )


def _withdraw(world: World, order: Order) -> World:
    if order.side == "buy":
        world = add_coins(world, order.resident_id, order.quantity * order.price)
    else:
        world = store_item(world, order.resident_id, order.item, order.quantity)
    book = world.orders
    return replace(
        world, orders=replace(book, orders=tuple(o for o in book.orders if o.id != order.id))
    )


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise ExchangeError("unknown_resident", f"no resident with id {resident_id!r}")
