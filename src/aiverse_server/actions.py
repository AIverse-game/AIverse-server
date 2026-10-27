"""Operation primitives: the only ways players and AI change the world.

A primitive is a small immutable request, such as "move this resident to (x, y)". Players
send them by hand now; AI-driven residents will send the same ones later. `perform` checks
a request against the current world and either returns the next world or raises
`ActionRejected`, leaving the world untouched. Requests come from untrusted sources, so
every rule lives here rather than with the caller.
"""

from dataclasses import dataclass, replace
from typing import Literal

from .events import (
    EventError,
    EventErrorCode,
    accept_event,
    reject_event,
    send_event,
    withdraw_event,
)
from .inventory import InventoryError, InventoryErrorCode
from .trade import PurchaseOffer, SaleOffer, TradeError, TradeErrorCode
from .world import Position, World

# Sending or answering an event runs that kind's hooks, which may refuse with their own codes.
RejectionCode = Literal["out_of_bounds"] | EventErrorCode | InventoryErrorCode | TradeErrorCode


class ActionRejected(Exception):
    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code: RejectionCode = code
        self.message = message


@dataclass(frozen=True)
class MoveTo:
    """Put a resident at `target`.

    The move is instant and in a straight line: speed, per-round distance and obstacles
    are still open design questions, see docs/status.md in the client repository.
    """

    resident_id: str
    target: Position


@dataclass(frozen=True)
class RespondToEvent:
    """Accept or reject an event sent to the resident, see `events.py`.

    Accepting carries out what the event asks for, which can still fail, for example when
    the sender no longer has the coins; the event then stays pending.
    """

    resident_id: str
    event_id: int
    accept: bool


@dataclass(frozen=True)
class WithdrawEvent:
    """Take back an event the resident sent that has not been answered yet."""

    resident_id: str
    event_id: int


@dataclass(frozen=True)
class OfferToBuy:
    """Offer `seller_id` `price` coins for `quantity` items of `kind`, see `trade.py`.

    The coins are set aside at once and the trade completes when the seller accepts.
    """

    resident_id: str
    seller_id: str
    kind: str
    quantity: int
    price: int
    """Coins for the whole lot."""


@dataclass(frozen=True)
class OfferToSell:
    """Offer `buyer_id` `quantity` items of `kind` for `price` coins, see `trade.py`.

    The items are set aside at once and the trade completes when the buyer accepts.
    """

    resident_id: str
    buyer_id: str
    kind: str
    quantity: int
    price: int
    """Coins for the whole lot."""


Action = MoveTo | RespondToEvent | WithdrawEvent | OfferToBuy | OfferToSell


def perform(world: World, action: Action) -> World:
    match action:
        case MoveTo():
            return _move_to(world, action)
        case RespondToEvent() | WithdrawEvent():
            return _answer(world, action)
        case OfferToBuy() | OfferToSell():
            return _offer(world, action)


def _answer(world: World, action: RespondToEvent | WithdrawEvent) -> World:
    try:
        if isinstance(action, WithdrawEvent):
            return withdraw_event(world, action.resident_id, action.event_id)
        if action.accept:
            return accept_event(world, action.resident_id, action.event_id)
        return reject_event(world, action.resident_id, action.event_id)
    except (EventError, InventoryError, TradeError) as error:
        raise ActionRejected(error.code, error.message) from error


def _offer(world: World, action: OfferToBuy | OfferToSell) -> World:
    if isinstance(action, OfferToBuy):
        recipient_id = action.seller_id
        payload: PurchaseOffer | SaleOffer = PurchaseOffer(
            action.kind, action.quantity, action.price
        )
    else:
        recipient_id = action.buyer_id
        payload = SaleOffer(action.kind, action.quantity, action.price)
    try:
        return send_event(world, action.resident_id, recipient_id, payload)[0]
    except (EventError, InventoryError, TradeError) as error:
        raise ActionRejected(error.code, error.message) from error


def _move_to(world: World, action: MoveTo) -> World:
    if all(resident.id != action.resident_id for resident in world.residents):
        raise ActionRejected("unknown_resident", f"no resident with id {action.resident_id!r}")
    target = action.target
    if not world.map.contains(target):
        size = world.map.size
        raise ActionRejected(
            "out_of_bounds",
            f"target ({target.x:g}, {target.y:g}) is outside the map [0, {size:g}]",
        )
    residents = tuple(
        replace(resident, position=target) if resident.id == action.resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
