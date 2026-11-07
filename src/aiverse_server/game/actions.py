"""Operation primitives: the only ways players and AI change the world.

A primitive is a small immutable request, such as "move this resident to (x, y)". Players
send them by hand now; AI-driven residents will send the same ones later. `perform` checks
a request against the current world and either returns the next world or raises
`ActionRejected`, leaving the world untouched. Requests come from untrusted sources, so
every rule lives here rather than with the caller. Every primitive names the resident who
acts, and none goes through for a resident who has left or once the game is over, or, except
leaving, speaking, withdrawing orders and handling compute and content, for a resident who is busy
walking. What a primitive
changes on the map goes into the activity log for others nearby to notice, see
`perception.py`.
"""

from dataclasses import dataclass
from typing import Literal

from ..core.compute import JobSource, NodeKind
from ..core.events import (
    EventError,
    EventErrorCode,
    accept_event,
    reject_event,
    send_event,
    withdraw_event,
)
from ..core.inventory import InventoryError, InventoryErrorCode
from ..core.orders import Side
from ..core.world import Position, World
from ..economy.ads import AdsError, AdsErrorCode, place_content
from ..economy.compute import (
    ComputeError,
    ComputeErrorCode,
    buy_node,
    start_build_ad,
    start_make_content,
)
from ..economy.contracts import (
    ContractError,
    ContractErrorCode,
    ContractProposal,
    cancel_contract,
    deliver,
)
from ..economy.exchange import (
    DEFAULT_LIFETIME,
    ExchangeError,
    ExchangeErrorCode,
    cancel_order,
    place_order,
)
from ..economy.land import LandError, LandErrorCode, buy_parcel
from ..economy.trade import PurchaseOffer, SaleOffer, TradeError, TradeErrorCode
from ..residents.information_offer import InformationError, InformationErrorCode, InformationOffer
from ..residents.memory import NoteError, NoteErrorCode
from ..residents.perception import record_activity
from ..residents.speech import SpeechError, SpeechErrorCode, say
from ..residents.walking import start_walk
from .lifecycle import GameError, GameErrorCode, check_can_act, check_not_busy, leave_game

# Sending or answering an event runs that kind's hooks, which may refuse with their own codes.
RejectionCode = (
    Literal["out_of_bounds"]
    | EventErrorCode
    | InventoryErrorCode
    | TradeErrorCode
    | GameErrorCode
    | SpeechErrorCode
    | ContractErrorCode
    | ExchangeErrorCode
    | LandErrorCode
    | ComputeErrorCode
    | AdsErrorCode
    | InformationErrorCode
    | NoteErrorCode
)


class ActionRejected(Exception):
    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code: RejectionCode = code
        self.message = message


@dataclass(frozen=True)
class MoveTo:
    """Set a resident walking to `target`; the walk costs time only.

    The walk is a straight line at a fixed speed, see `walking.py`; the resident is busy
    until they arrive. Per-round distance and obstacles are still open design questions, see
    docs/status.md in the client repository.
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
class OfferInformation:
    """Offer `buyer_id` a copy of what the resident knows for `price` coins, see
    `information_offer.py`.

    The buyer sees only `summary` until they accept; then they pay and get `content` as a
    note. Nothing is set aside and the seller keeps the information.
    """

    resident_id: str
    buyer_id: str
    summary: str
    content: str
    price: int
    """Coins for the information."""


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
class BuyParcel:
    """Buy the parcel of land the resident stands in from the town, see `land.py`."""

    resident_id: str


@dataclass(frozen=True)
class BuyNode:
    """Subscribe to a node of `kind` for `seconds`, paying the system, from anywhere, see
    `compute.py`."""

    resident_id: str
    kind: NodeKind
    seconds: int


@dataclass(frozen=True)
class BuildAd:
    """Start building an ad slot on the resident's own bare parcel `parcel_id` from anywhere,
    on their own compute, a node hired for the job, or the subscribed node `node_id`, see
    `compute.py`. The resident need not stay: the ad slot appears when the job ends."""

    resident_id: str
    parcel_id: str
    source: JobSource
    node_id: int | None = None


@dataclass(frozen=True)
class MakeContent:
    """Start rendering a piece of content from anywhere, on the same sources as `BuildAd`
    but a render node, see `compute.py`. The resident need not stay: the piece joins what
    they carry when the job ends."""

    resident_id: str
    source: JobSource
    node_id: int | None = None


@dataclass(frozen=True)
class PlaceContent:
    """Put one piece of content they carry into the ad slot on the resident's own parcel
    `parcel_id`, from anywhere, see `ads.py`."""

    resident_id: str
    parcel_id: str


@dataclass(frozen=True)
class PlaceOrder:
    """Place an order on the exchange from anywhere, see `exchange.py`: it trades at once with
    the best orders on the other side and rests for `lifetime` seconds with whatever is left,
    holding the coins or items it needs."""

    resident_id: str
    side: Side
    item: str
    quantity: int
    price: int
    """Coins per unit."""
    lifetime: float = DEFAULT_LIFETIME


@dataclass(frozen=True)
class CancelOrder:
    """Withdraw one of the resident's resting orders, from anywhere and even while busy, and
    get back what it still holds."""

    resident_id: str
    order_id: int


@dataclass(frozen=True)
class Say:
    """Say `text` aloud to everyone within earshot, or, given `to`, to that one resident
    wherever they are, see `speech.py`. Works while walking."""

    resident_id: str
    text: str
    to: str | None = None


Action = (
    MoveTo
    | RespondToEvent
    | WithdrawEvent
    | OfferToBuy
    | OfferToSell
    | OfferInformation
    | ProposeContract
    | Deliver
    | CancelContract
    | LeaveGame
    | BuyParcel
    | BuyNode
    | BuildAd
    | MakeContent
    | PlaceContent
    | PlaceOrder
    | CancelOrder
    | Say
)


def perform(world: World, action: Action) -> World:
    try:
        check_can_act(world, action.resident_id)
        if not isinstance(
            action,
            LeaveGame | Say | CancelOrder | BuyNode | BuildAd | MakeContent | PlaceContent,
        ):
            check_not_busy(world, action.resident_id)
    except GameError as error:
        raise ActionRejected(error.code, error.message) from error
    return record_activity(world, _carry_out(world, action))


def _carry_out(world: World, action: Action) -> World:
    match action:
        case MoveTo():
            return _move_to(world, action)
        case RespondToEvent() | WithdrawEvent():
            return _answer(world, action)
        case OfferToBuy() | OfferToSell():
            return _offer(world, action)
        case OfferInformation():
            offer = InformationOffer(action.summary, action.content, action.price)
            try:
                return send_event(world, action.resident_id, action.buyer_id, offer)[0]
            except (EventError, InformationError) as error:
                raise ActionRejected(error.code, error.message) from error
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
        case BuyParcel():
            try:
                return buy_parcel(world, action.resident_id)
            except (LandError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case BuyNode():
            try:
                return buy_node(world, action.resident_id, action.kind, action.seconds)
            except (ComputeError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case BuildAd():
            try:
                return start_build_ad(
                    world, action.resident_id, action.parcel_id, action.source, action.node_id
                )
            except (ComputeError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case MakeContent():
            try:
                return start_make_content(world, action.resident_id, action.source, action.node_id)
            except (ComputeError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case PlaceContent():
            try:
                return place_content(world, action.resident_id, action.parcel_id)
            except (AdsError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case PlaceOrder():
            try:
                return place_order(
                    world,
                    action.resident_id,
                    action.side,
                    action.item,
                    action.quantity,
                    action.price,
                    action.lifetime,
                )
            except (ExchangeError, InventoryError) as error:
                raise ActionRejected(error.code, error.message) from error
        case CancelOrder():
            try:
                return cancel_order(world, action.resident_id, action.order_id)
            except ExchangeError as error:
                raise ActionRejected(error.code, error.message) from error
        case Say():
            try:
                return say(world, action.resident_id, action.text, action.to)
            except SpeechError as error:
                raise ActionRejected(error.code, error.message) from error


def _answer(world: World, action: RespondToEvent | WithdrawEvent) -> World:
    try:
        if isinstance(action, WithdrawEvent):
            return withdraw_event(world, action.resident_id, action.event_id)
        if action.accept:
            return accept_event(world, action.resident_id, action.event_id)
        return reject_event(world, action.resident_id, action.event_id)
    except (
        EventError,
        InventoryError,
        TradeError,
        ContractError,
        InformationError,
        NoteError,
    ) as error:
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
    return start_walk(world, action.resident_id, target)
