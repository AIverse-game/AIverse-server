"""Operation primitives: the only ways players and AI change the world.

A primitive is a small immutable request, such as "move this resident to (x, y)". Players
send them by hand now; AI-driven residents will send the same ones later. `perform` checks
a request against the current world and either returns the next world or raises
`ActionRejected`, leaving the world untouched. Requests come from untrusted sources, so
every rule lives here rather than with the caller. Every primitive names the resident who
acts, and none goes through for a resident who has left or once the game is over, or, except
leaving, for a resident who is mining. A resident whose last token is spent by a primitive,
their own or one they agreed to, is out of the game afterwards, see `lifecycle.py`.
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
from .lifecycle import GameError, GameErrorCode, check_can_act, knock_out_exhausted, leave_game
from .mining import MiningError, MiningErrorCode, check_not_busy, start_mining
from .tokens import TokenError, TokenErrorCode, pay_for_walk
from .trade import PurchaseOffer, SaleOffer, TradeError, TradeErrorCode
from .world import Position, World

# Sending or answering an event runs that kind's hooks, which may refuse with their own codes.
RejectionCode = (
    Literal["out_of_bounds"]
    | EventErrorCode
    | InventoryErrorCode
    | TradeErrorCode
    | GameErrorCode
    | TokenErrorCode
    | MiningErrorCode
)


class ActionRejected(Exception):
    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code: RejectionCode = code
        self.message = message


@dataclass(frozen=True)
class MoveTo:
    """Put a resident at `target`, paying for the walk in tokens, see `tokens.py`.

    The move is instant and in a straight line: speed, per-round distance and obstacles
    are still open design questions, see docs/status.md in the client repository. A walk
    the resident cannot pay for is refused; one that spends their last token takes them
    out of the game.
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


@dataclass(frozen=True)
class LeaveGame:
    """Leave the game for good, see `lifecycle.py`: pending offers are called off, items are
    lost and coins are kept for the ranking. The game goes on for everyone else."""

    resident_id: str


@dataclass(frozen=True)
class Mine:
    """Spend time at a token pool the resident stands next to, see `mining.py`: the tokens
    arrive when the job ends, and until then the resident can do nothing but leave."""

    resident_id: str
    pool_id: str


Action = MoveTo | RespondToEvent | WithdrawEvent | OfferToBuy | OfferToSell | LeaveGame | Mine


def perform(world: World, action: Action) -> World:
    try:
        check_can_act(world, action.resident_id)
        if not isinstance(action, LeaveGame):
            check_not_busy(world, action.resident_id)
    except (GameError, MiningError) as error:
        raise ActionRejected(error.code, error.message) from error
    return knock_out_exhausted(world, _carry_out(world, action))


def _carry_out(world: World, action: Action) -> World:
    match action:
        case MoveTo():
            return _move_to(world, action)
        case RespondToEvent() | WithdrawEvent():
            return _answer(world, action)
        case OfferToBuy() | OfferToSell():
            return _offer(world, action)
        case LeaveGame():
            return leave_game(world, action.resident_id)
        case Mine():
            try:
                return start_mining(world, action.resident_id, action.pool_id)
            except MiningError as error:
                raise ActionRejected(error.code, error.message) from error


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
    target = action.target
    if not world.map.contains(target):
        size = world.map.size
        raise ActionRejected(
            "out_of_bounds",
            f"target ({target.x:g}, {target.y:g}) is outside the map [0, {size:g}]",
        )
    start = next(r.position for r in world.residents if r.id == action.resident_id)
    try:
        world = pay_for_walk(world, action.resident_id, start, target)
    except TokenError as error:
        raise ActionRejected(error.code, error.message) from error
    residents = tuple(
        replace(resident, position=target) if resident.id == action.resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
