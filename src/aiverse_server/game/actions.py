"""Operation primitives: the only ways players and AI change the world.

A primitive is a small immutable request, such as "move this resident to (x, y)". Players
send them by hand now; AI-driven residents will send the same ones later. `perform` checks
a request against the current world and either returns the next world or raises
`ActionRejected`, leaving the world untouched. Requests come from untrusted sources, so
every rule lives here rather than with the caller. Every primitive names the resident who
acts, and none goes through for a resident who has left or once the game is over, or, except
leaving and speaking, for a resident who is busy mining or walking. A resident whose last
token is spent by a primitive, their own or one they agreed to, is out of the game
afterwards, see `lifecycle.py`. What a primitive changes on the map goes into the activity
log for others nearby to notice, see `perception.py`.
"""

from dataclasses import dataclass
from typing import Literal

from ..core.events import (
    EventError,
    EventErrorCode,
    accept_event,
    reject_event,
    send_event,
    withdraw_event,
)
from ..core.inventory import InventoryError, InventoryErrorCode
from ..core.world import Position, World
from ..economy.chips import ChipError, ChipErrorCode, buy_chip
from ..economy.contracts import (
    ContractError,
    ContractErrorCode,
    ContractProposal,
    cancel_contract,
    deliver,
)
from ..economy.market import MarketError, MarketErrorCode, sell_tokens
from ..economy.mining import MiningError, MiningErrorCode, check_not_busy, start_mining
from ..economy.tokens import TokenError, TokenErrorCode, pay_for_walk
from ..economy.trade import PurchaseOffer, SaleOffer, TradeError, TradeErrorCode
from ..residents.perception import record_activity
from ..residents.speech import SpeechError, SpeechErrorCode, say
from ..residents.walking import start_walk
from .lifecycle import GameError, GameErrorCode, check_can_act, knock_out_exhausted, leave_game

# Sending or answering an event runs that kind's hooks, which may refuse with their own codes.
RejectionCode = (
    Literal["out_of_bounds"]
    | EventErrorCode
    | InventoryErrorCode
    | TradeErrorCode
    | GameErrorCode
    | TokenErrorCode
    | MiningErrorCode
    | MarketErrorCode
    | ChipErrorCode
    | SpeechErrorCode
    | ContractErrorCode
)


class ActionRejected(Exception):
    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code: RejectionCode = code
        self.message = message


@dataclass(frozen=True)
class MoveTo:
    """Set a resident walking to `target`, paying for the walk in tokens, see `tokens.py`.

    The walk is a straight line at a fixed speed, see `walking.py`; the resident is busy
    until they arrive. Per-round distance and obstacles are still open design questions, see
    docs/status.md in the client repository. A walk the resident cannot pay for is refused;
    one that spends their last token takes them out of the game when they arrive.
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
class ProposeContract:
    """Propose a contract to `partner_id`, see `contracts.py`: the supplier is to deliver
    `quantity` items of `kind` `deliveries` times within `duration` seconds of the partner
    accepting, each delivery paying them `price` coins, staking `deposit` coins on each. The
    resident supplies if `supplies`, and otherwise buys; their coins for every delivery,
    deposit or payment, are set aside at once."""

    resident_id: str
    partner_id: str
    supplies: bool
    kind: str
    quantity: int
    price: int
    """Coins for one delivery."""
    deliveries: int
    duration: int
    deposit: int = 0
    """Coins the supplier stakes on one delivery."""


@dataclass(frozen=True)
class Deliver:
    """As the supplier, hand the buyer one delivery under a contract, be paid for it from the
    coins they set aside and get its deposit back, see `contracts.py`."""

    resident_id: str
    contract_id: int


@dataclass(frozen=True)
class CancelContract:
    """As the supplier, call off a contract's remaining deliveries, forfeiting their deposit
    to the buyer, see `contracts.py`."""

    resident_id: str
    contract_id: int


@dataclass(frozen=True)
class LeaveGame:
    """Leave the game for good, see `lifecycle.py`: pending offers are called off, items are
    lost and coins are kept for the ranking. The game goes on for everyone else."""

    resident_id: str


@dataclass(frozen=True)
class Mine:
    """Spend time at a token pool the resident stands next to with the best chip
    they carry, see `mining.py`: the tokens arrive when the job ends, and until then the
    resident stays with the chip and can do nothing but leave."""

    resident_id: str
    pool_id: str


@dataclass(frozen=True)
class SellTokens:
    """Sell `quantity` carried tokens to the market the resident stands at, for coins at the
    market price, see `market.py`. Selling the last token owned takes them out of the game."""

    resident_id: str
    quantity: int


@dataclass(frozen=True)
class BuyChip:
    """Buy one chip of `model` at the market the resident stands at, for its fixed
    price in coins, see `chips.py`."""

    resident_id: str
    model: str


@dataclass(frozen=True)
class Say:
    """Say `text` aloud to everyone within earshot, or, given `to`, to that one resident
    wherever they are, see `speech.py`. Costs a few carried tokens, more aloud than to one
    resident, and works while walking or mining."""

    resident_id: str
    text: str
    to: str | None = None


Action = (
    MoveTo
    | RespondToEvent
    | WithdrawEvent
    | OfferToBuy
    | OfferToSell
    | ProposeContract
    | Deliver
    | CancelContract
    | LeaveGame
    | Mine
    | SellTokens
    | BuyChip
    | Say
)


def perform(world: World, action: Action) -> World:
    try:
        check_can_act(world, action.resident_id)
        if not isinstance(action, LeaveGame | Say):
            check_not_busy(world, action.resident_id)
    except (GameError, MiningError) as error:
        raise ActionRejected(error.code, error.message) from error
    return record_activity(world, knock_out_exhausted(world, _carry_out(world, action)))


def _carry_out(world: World, action: Action) -> World:
    match action:
        case MoveTo():
            return _move_to(world, action)
        case RespondToEvent() | WithdrawEvent():
            return _answer(world, action)
        case OfferToBuy() | OfferToSell():
            return _offer(world, action)
        case ProposeContract():
            payload = ContractProposal(
                action.supplies,
                action.kind,
                action.quantity,
                action.price,
                action.deliveries,
                action.duration,
                action.deposit,
            )
            try:
                return send_event(world, action.resident_id, action.partner_id, payload)[0]
            except (EventError, InventoryError, ContractError) as error:
                raise ActionRejected(error.code, error.message) from error
        case Deliver() | CancelContract():
            carry_out = deliver if isinstance(action, Deliver) else cancel_contract
            try:
                return carry_out(world, action.resident_id, action.contract_id)
            except (ContractError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case LeaveGame():
            return leave_game(world, action.resident_id)
        case Mine():
            try:
                return start_mining(world, action.resident_id, action.pool_id)
            except MiningError as error:
                raise ActionRejected(error.code, error.message) from error
        case SellTokens():
            try:
                return sell_tokens(world, action.resident_id, action.quantity)
            except (MarketError, TokenError) as error:
                raise ActionRejected(error.code, error.message) from error
        case BuyChip():
            try:
                return buy_chip(world, action.resident_id, action.model)
            except (ChipError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case Say():
            try:
                return say(world, action.resident_id, action.text, action.to)
            except (SpeechError, TokenError) as error:
                raise ActionRejected(error.code, error.message) from error


def _answer(world: World, action: RespondToEvent | WithdrawEvent) -> World:
    try:
        if isinstance(action, WithdrawEvent):
            return withdraw_event(world, action.resident_id, action.event_id)
        if action.accept:
            return accept_event(world, action.resident_id, action.event_id)
        return reject_event(world, action.resident_id, action.event_id)
    except (EventError, InventoryError, TradeError, ContractError) as error:
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
    return start_walk(world, action.resident_id, target)
