"""Wire format shared with the web client (aiverse-client/packages/protocol).

Messages are JSON text frames with camelCase keys. Keep both sides in step and bump
PROTOCOL_VERSION on incompatible changes.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from ..agents.llm_brain import LLMSetting, LLMSettings
from ..agents.runner import TurnRecord
from ..core.events import Event
from ..core.orders import Fill
from ..core.world import COLOR_PATTERN, Position, World
from ..economy.ads import shown
from ..economy.contracts import ContractProposal, contracts_of
from ..economy.exchange import EXCHANGE_ITEMS, MAX_ORDERS, depth
from ..economy.land import PARCEL_SIZE, grid_size, plots
from ..economy.payouts import latest_payout
from ..economy.trade import PurchaseOffer, SaleOffer
from ..economy.visitors import VISITOR_SPEED, remaining_legs, visitor_position
from ..game.actions import (
    Action,
    ActionRejected,
    BuildAd,
    BuildPortal,
    BuildVenue,
    BuyParcel,
    LeaveGame,
    MakeContent,
    MoveTo,
    PlaceContent,
    RejectionCode,
    Say,
)
from ..game.cues import CueKind
from ..game.lifecycle import GameErrorCode, ranks, total_coins
from ..residents.walking import position_at

PROTOCOL_VERSION = 30

ERROR_MAX_LENGTH = 200

SPEECH_VIEW_LIMIT = 50
"""Latest lines of speech each state carries, so a long game does not grow every state."""

BOOK_LEVELS_SHOWN = 5
"""Best prices on each side of an item's book each state carries."""

FILL_VIEW_LIMIT = 100
"""Latest exchange fills each state carries, of every item together; they make up most of a
state once the exchange is busy, so pages get only enough for a short price line."""


class _Message(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


# Client -> server. Untrusted, so validated on arrival; unknown keys are ignored.

# Strict so that booleans and numeric strings are refused; NaN and infinities too.
Coordinate = Annotated[StrictFloat, Field(allow_inf_nan=False)]


class TargetInput(_Message):
    x: Coordinate
    y: Coordinate


class MoveToInput(_Message):
    type: Literal["move_to"]
    resident_id: str
    target: TargetInput


class LeaveGameInput(_Message):
    type: Literal["leave_game"]
    resident_id: str


class SayInput(_Message):
    type: Literal["say"]
    resident_id: str
    text: str
    to: str | None = None
    """The one resident spoken to; left out to speak aloud to everyone within earshot."""


class BuyParcelInput(_Message):
    type: Literal["buy_parcel"]
    resident_id: str


class BuildFacilityInput(_Message):
    type: Literal["build_facility"]
    resident_id: str
    parcel_id: str
    kind: Literal["ad", "portal", "game_hall", "workspace", "mall"]
    source: Literal["self", "on_demand"]
    orientation: Literal["north_south", "east_west"] | None = None
    """Which way a mall runs; north to south when not said."""


class MakeContentInput(_Message):
    type: Literal["make_content"]
    resident_id: str
    content: Literal["ad", "game"]
    source: Literal["self", "on_demand"]


class PlaceContentInput(_Message):
    type: Literal["place_content"]
    resident_id: str
    parcel_id: str


ActionInput = Annotated[
    MoveToInput
    | LeaveGameInput
    | SayInput
    | BuyParcelInput
    | BuildFacilityInput
    | MakeContentInput
    | PlaceContentInput,
    Field(discriminator="type"),
]


class HelloMessage(_Message):
    type: Literal["hello"]
    protocol_version: StrictInt


class ViewAsMessage(_Message):
    """Choose whose whispers, offers, contracts and exchange orders this connection sees: the
    resident's, or none for None. Pages send it when the player picks a resident; there is no
    ownership yet, so any resident may be chosen."""

    type: Literal["view_as"]
    resident_id: str | None


class ActionMessage(_Message):
    type: Literal["action"]
    # Only the primitives clients can use so far; pages see offers, contracts and the
    # exchange now, but making and answering offers and placing orders is still left to agents.
    action: ActionInput


# Long enough for any real URL, key or model name; a bound so one frame cannot pin much memory.
SETTING_MAX_LENGTH = 2048

SettingInput = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=SETTING_MAX_LENGTH)
]


class LLMSettingsInput(_Message):
    """What the player filled in; blank or left out means not given, which is fine for those
    the server overrides."""

    base_url: SettingInput | None = None
    api_key: SettingInput | None = Field(default=None, repr=False)
    model: SettingInput | None = None


class StartAgentMessage(_Message):
    """Hand the resident to a language model with these settings, replacing any agent driving
    them. There is no ownership yet, so any resident may be chosen."""

    type: Literal["start_agent"]
    resident_id: str
    llm: LLMSettingsInput
    strategy: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""


class StopAgentMessage(_Message):
    """Stop the agent driving the resident, if any, leaving them to the pages."""

    type: Literal["stop_agent"]
    resident_id: str


ClientMessage = Annotated[
    HelloMessage | ViewAsMessage | ActionMessage | StartAgentMessage | StopAgentMessage,
    Field(discriminator="type"),
]

_client_message: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# Server -> client.


class PositionView(_Message):
    x: float
    y: float


class WalkingView(_Message):
    target: PositionView
    remaining_seconds: float
    """Time left until arrival when this was sent."""


@dataclass(frozen=True)
class AgentMood:
    """What an agent's resident is showing, kept by the game for `AgentView`."""

    thinking: bool = False
    cue: CueKind | None = None
    cue_seq: int = 0


class CueView(_Message):
    """A moment the resident acted out: spoke to someone, or something worked."""

    kind: CueKind
    seq: int
    """Counts up over the whole game; a page plays a cue when it sees a `seq` it has not."""


class AgentView(_Message):
    """Present on a resident an agent drives."""

    model: str | None = None
    """The language model asking, as the server calls it; left out for a scripted brain."""
    thinking: Literal[True] | None = None
    """Present while the agent waits for its brain's answer."""
    cue: CueView | None = None
    """The latest talk or success of the resident, absent before the first."""


class ResidentView(_Message):
    id: str
    name: str
    color: str = Field(pattern=f"^{COLOR_PATTERN.pattern}$")
    position: PositionView
    """Where the resident is when this was sent, on the way while walking."""
    coins: int
    spendable_coins: int
    """Total coins, including those set aside by pending offers, contracts and buy orders;
    public to everyone."""
    left: bool
    rank: int
    """Place by coins, 1 for the most; equal totals share a place. Final once the game ends."""
    walking: WalkingView | None = None
    """Present while the resident is walking; they head straight for `target` at a steady
    pace and arrive after `remaining_seconds`."""
    agent: AgentView | None = None
    """Present while an agent drives the resident rather than the pages."""


class LevelView(_Message):
    price: int
    """Coins per unit."""
    quantity: int
    """Units resting at that price, of every order together."""


class BookView(_Message):
    item: str
    bids: list[LevelView]
    """The best `BOOK_LEVELS_SHOWN` buy prices, highest first."""
    asks: list[LevelView]
    """The best `BOOK_LEVELS_SHOWN` sell prices, lowest first."""


class FillView(_Message):
    """Units that changed hands on the exchange, without who traded."""

    seq: int
    time: float
    """Game seconds since the start, to a tenth of a second."""
    item: str
    quantity: int
    price: int
    """Coins per unit."""
    own: Literal["bought", "sold"] | None = None
    """Present when the resident the connection views as took part."""


class OrderView(_Message):
    """A resting order of the resident the connection views as."""

    id: int
    side: Literal["buy", "sell"]
    item: str
    quantity: int
    """Units still wanted or offered."""
    price: int
    """Coins per unit."""
    remaining_seconds: float
    """Time until it expires when this was sent."""


class ExchangeView(_Message):
    """The anonymous exchange, see `exchange.py`."""

    books: list[BookView]
    """One per item traded, in a fixed order."""
    fills: list[FillView]
    """The latest `FILL_VIEW_LIMIT` fills of every item, oldest first."""
    viewer_id: str | None = None
    """The resident the connection viewed as when this was sent, whose orders `orders` lists
    and whose fills `own` marks; left out for nobody. A page that has since chosen someone
    else can tell this state is not about them yet."""
    orders: list[OrderView]
    """Resting orders of the resident the connection views as, oldest first; none when it views
    as nobody. Others' orders show only as totals in the books."""
    max_orders: int
    """Resting orders a resident may have at once."""


class SpeechView(_Message):
    """A line of speech: one spoken aloud, or a whisper to or from the resident the
    connection views as. Residents themselves hear only what reached them, see `speech.py`."""

    seq: int
    time: float
    """Game seconds since the start when it was said."""
    speaker_id: str
    text: str
    to: str | None = None
    """The one resident spoken to; left out when spoken aloud."""
    listeners: list[str]
    """Who heard it, not counting the speaker."""


class OfferView(_Message):
    """A trade offer waiting for an answer, see `trade.py`."""

    event_id: int
    side: Literal["buy", "sell"]
    """`buy` when the sender offers to buy, `sell` when the sender offers to sell."""
    from_id: str
    to_id: str
    item: str
    quantity: int
    price: int
    """Coins for the whole lot."""


class ContractProposalView(_Message):
    """A contract proposal waiting for an answer, see `contracts.py`."""

    event_id: int
    from_id: str
    to_id: str
    supplier_id: str
    buyer_id: str
    item: str
    quantity: int
    """Items in each delivery."""
    price: int
    """Coins for each delivery."""
    deliveries: int
    duration_seconds: int
    """How long the contract runs once accepted."""
    deposit: int
    """Coins the supplier stakes on each delivery; 0 for none."""


class ContractView(_Message):
    """A contract in force, see `contracts.py`."""

    id: int
    supplier_id: str
    buyer_id: str
    item: str
    quantity: int
    price: int
    deliveries_left: int
    escrow: int
    """The buyer's coins set aside for the deliveries left."""
    deposit: int
    """Coins the supplier stakes on each delivery; 0 for none."""
    deposit_held: int
    """The supplier's coins set aside for the deliveries left, the buyer's if not made."""
    remaining_seconds: float
    """Time until it lapses when this was sent."""


class MapView(_Message):
    size: float
    parcel_size: float
    block_cells: int
    """Parcels along each side of a block, which is bought whole."""
    venue_cells: int
    """Parcels along each side of the square a game hall or a workspace takes up, with its
    north-west corner on the parcel it is built on. A mall takes up 1 x 2, west to east and
    north to south."""
    lobby: bool
    """Whether the middle block is the spawn lobby, which nobody owns."""
    parcel_prices: list[list[int]]


class IncomeView(_Message):
    at: float
    """Game time of the payday that paid the facility; a later one than the page last saw is new."""
    coins: int
    """Whole coins the facility was paid then, sponsors and admission fees together."""


class FacilityView(_Message):
    kind: Literal["ad", "portal", "game_hall", "workspace", "mall"]
    contents: int
    expires_in: list[float]
    orientation: Literal["north_south", "east_west"] = "north_south"
    """Which way a mall runs: north_south takes the parcel and the one south of it, east_west the
    parcel and the one east of it."""
    income: IncomeView | None = None
    """What the facility was paid at the payday that last paid it; absent when none has."""


class ParcelView(_Message):
    id: str
    col: int
    row: int
    owner_id: str
    shares: dict[str, int]
    facility: FacilityView | None = None


class JobView(_Message):
    id: int
    owner_id: str
    kind: str
    parcel_id: str | None = None
    remaining_seconds: float
    orientation: Literal["north_south", "east_west"] = "north_south"
    """Which way the mall being built runs."""


class ContentView(_Message):
    viewer_id: str
    ad_content: int
    game_content: int


class GameView(_Message):
    ended: bool
    remaining_seconds: float
    """Time left when this was sent; 0 once the game has ended."""


class ResourceBalanceView(_Message):
    coins: int


class AgentCallView(_Message):
    name: str
    arguments: str
    time: float
    ok: bool
    code: str | None = None
    coins_change: int


class AgentTurnView(_Message):
    seq: int
    time: float
    ended_at: float
    summary: str | None = None
    error: str | None = None
    before: ResourceBalanceView
    after: ResourceBalanceView
    calls: list[AgentCallView]


class AgentTimelineView(_Message):
    resident_id: str
    turns: list[AgentTurnView]


# Latest completed rounds per resident. Summaries and arguments are bounded separately.
AGENT_TURN_LIMIT = 30


def agent_turn_view(
    turn: TurnRecord, seq: int, redact: Callable[[str], str] = lambda text: text
) -> AgentTurnView:
    assert turn.before is not None and turn.after is not None
    return AgentTurnView(
        seq=seq,
        time=turn.time,
        ended_at=turn.ended_at,
        summary=redact(turn.remark)[:500] if turn.remark else None,
        # Exception text is diagnostic, may contain private input, and stays in server logs.
        error="本轮 AI 调用失败，请查看服务端日志。" if turn.error else None,
        before=ResourceBalanceView(coins=turn.before.coins),
        after=ResourceBalanceView(coins=turn.after.coins),
        calls=[
            AgentCallView(
                name=redact(call.name)[:80],
                # Redact before repr escapes characters; never display malformed raw input.
                arguments=(
                    "参数不是有效 JSON，未展示原文。"
                    if "_raw" in call.arguments and call.result.code == "invalid_arguments"
                    else repr(_redacted_arguments(call.arguments, redact))[:300]
                ),
                time=call.time,
                ok=call.result.ok,
                code=call.result.code,
                coins_change=call.coins_change,
            )
            for call in turn.calls[:128]
        ],
    )


def _redacted_arguments(value: object, redact: Callable[[str], str]) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {redact(str(key)): _redacted_arguments(item, redact) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted_arguments(item, redact) for item in value]
    return value


class WorldView(_Message):
    map: MapView
    parcels: list[ParcelView]
    jobs: list[JobView]
    content: ContentView | None = None
    residents: list[ResidentView]
    game: GameView
    exchange: ExchangeView
    speech: list[SpeechView]
    """The latest `SPEECH_VIEW_LIMIT` lines the connection may see, oldest first: every line
    spoken aloud, and the whispers to or from the resident it views as."""
    offers: list[OfferView]
    """Trade offers to or from the resident the connection views as, oldest first; none
    when it views as nobody. Others' deals are private, like whispers."""
    contract_proposals: list[ContractProposalView]
    """Contract proposals to or from the viewed resident, oldest first."""
    contracts: list[ContractView]
    """Contracts in force the viewed resident is a party to, oldest first."""
    agent_timeline: AgentTimelineView | None = None


LLMSettingName = Literal["baseUrl", "apiKey", "model"]

_SETTING_NAMES: dict[LLMSetting, LLMSettingName] = {
    "base_url": "baseUrl",
    "api_key": "apiKey",
    "model": "model",
}


class WelcomeMessage(_Message):
    type: Literal["welcome"] = "welcome"
    protocol_version: int = PROTOCOL_VERSION
    llm_overrides: list[LLMSettingName] = []
    """Language model settings the server's config fixes for every agent, so pages need not
    ask for them; only which ones, never their values."""


class StateMessage(_Message):
    type: Literal["state"] = "state"
    world: WorldView


class LegView(_Message):
    """A straight stretch of a visitor's walk. When a walk has several, the next may start
    elsewhere than this one ends: the visitor went through a portal, which takes no time."""

    start: PositionView
    end: PositionView


class VisitorView(_Message):
    id: int
    position: PositionView
    """Where the visitor is when this was sent."""
    legs: list[LegView] = []
    """The stretches it has still to walk, at `VisitorsMessage.speed`, the first one starting at
    `position`; empty while it stands. Past the last it stands at the end of it."""


class VisitorsMessage(_Message):
    """The scripted visitors on the map, which move on all the time and so are not part of a
    `state`. Each one is a snapshot to play on locally: it holds until the next `visitors`
    message, which replaces it; a visitor missing from that one has left the map. Sent to every
    greeted connection straight after `state` on hello, then every `VISITOR_PUSH_SECONDS`."""

    type: Literal["visitors"] = "visitors"
    speed: float
    """Map units a walking visitor covers per second."""
    visitors: list[VisitorView]


class ActionResultMessage(_Message):
    """Reply to the sender's `action`. On success the new world follows as a `state`
    sent to every connection; `code` and `message` are present only on rejection."""

    type: Literal["action_result"] = "action_result"
    ok: bool
    code: RejectionCode | None = None
    message: str | None = Field(default=None, max_length=ERROR_MAX_LENGTH)


AgentRejectionCode = GameErrorCode | Literal["missing_llm_setting", "invalid_llm_setting"]


class AgentResultMessage(_Message):
    """Reply to the sender's `start_agent` or `stop_agent` for `resident_id`. On success every
    connection receives a `state` showing who drives the resident; `code` and `message` are
    present only on rejection."""

    type: Literal["agent_result"] = "agent_result"
    resident_id: str
    ok: bool
    code: AgentRejectionCode | None = None
    message: str | None = Field(default=None, max_length=ERROR_MAX_LENGTH)


ErrorCode = Literal["bad_message", "protocol_mismatch"]


class ErrorMessage(_Message):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str = Field(max_length=ERROR_MAX_LENGTH)


ServerMessage = (
    WelcomeMessage
    | StateMessage
    | VisitorsMessage
    | ActionResultMessage
    | AgentResultMessage
    | ErrorMessage
)


def income_view(world: World, parcel_id: str) -> IncomeView | None:
    """What the facility on the parcel was paid at its latest payday, None when never."""
    payout = latest_payout(world, parcel_id)
    return None if payout is None else IncomeView(at=payout.at, coins=payout.coins)


def _agent_view(model: str | None, mood: AgentMood | None) -> AgentView:
    if mood is None:
        return AgentView(model=model)
    cue = None if mood.cue is None else CueView(kind=mood.cue, seq=mood.cue_seq)
    return AgentView(model=model, thinking=True if mood.thinking else None, cue=cue)


def world_view(
    world: World,
    remaining_seconds: float,
    viewer: str | None = None,
    agents: Mapping[str, str | None] | None = None,
    agent_turns: Mapping[str, Sequence[AgentTurnView]] | None = None,
    agent_moods: Mapping[str, AgentMood] | None = None,
) -> WorldView:
    """The world as a connection viewing as resident `viewer`, or as nobody, sees it: the
    same for everyone except whispers, offers, contracts and exchange orders, of which it sees
    only the viewer's. `agents` maps
    the residents agents drive to the model each asks, None for a scripted brain, and
    `agent_moods` what each is showing."""
    driven = agents or {}
    moods = agent_moods or {}
    places = ranks(world)
    deals = [
        event
        for event in world.events.pending
        if viewer is not None and viewer in (event.sender_id, event.recipient_id)
    ]
    visible = [
        line
        for line in world.speech.entries
        if line.to is None or (viewer is not None and viewer in (line.speaker_id, line.to))
    ]
    viewed_resident = next((r for r in world.residents if r.id == viewer), None)
    squares = plots(world)
    side_count = grid_size(world)
    return WorldView(
        agent_timeline=AgentTimelineView(
            resident_id=viewer, turns=list((agent_turns or {}).get(viewer, ()))
        )
        if viewer is not None
        else None,
        map=MapView(
            size=world.map.size,
            parcel_size=PARCEL_SIZE,
            block_cells=world.map.block_cells,
            venue_cells=world.map.venue_cells,
            lobby=world.map.lobby,
            parcel_prices=[
                [square.price for square in squares[row * side_count : (row + 1) * side_count]]
                for row in range(side_count)
            ],
        ),
        parcels=[
            ParcelView(
                id=parcel.id,
                col=parcel.col,
                row=parcel.row,
                owner_id=parcel.owner_id,
                shares=dict(parcel.shares),
                facility=FacilityView(
                    kind=parcel.facility.kind,
                    contents=shown(parcel.facility, world.time),
                    expires_in=[
                        stale_at - world.time
                        for stale_at in parcel.facility.contents
                        if stale_at > world.time
                    ],
                    orientation=parcel.facility.orientation,
                    income=income_view(world, parcel.id),
                )
                if parcel.facility is not None
                else None,
            )
            for parcel in world.parcels
        ],
        jobs=[
            JobView(
                id=job.id,
                owner_id=job.owner_id,
                kind=job.kind,
                parcel_id=job.parcel_id,
                remaining_seconds=max(job.ends_at - world.time, 0.0),
                orientation=job.orientation,
            )
            for job in world.compute.jobs
        ],
        content=ContentView(
            viewer_id=viewer,
            ad_content=viewed_resident.inventory.count("ad_content"),
            game_content=viewed_resident.inventory.count("game_content"),
        )
        if viewer is not None and viewed_resident is not None
        else None,
        residents=[
            ResidentView(
                id=resident.id,
                name=resident.name,
                color=resident.color,
                position=_position_view(position_at(resident, world.time)),
                coins=total_coins(world, resident.id),
                spendable_coins=resident.coins,
                left=resident.left,
                rank=places[resident.id],
                walking=None
                if resident.walking is None
                else WalkingView(
                    target=_position_view(resident.walking.target),
                    remaining_seconds=max(resident.walking.arrives_at - world.time, 0.0),
                ),
                agent=_agent_view(driven[resident.id], moods.get(resident.id))
                if resident.id in driven
                else None,
            )
            for resident in world.residents
        ],
        game=GameView(
            ended=world.ended,
            remaining_seconds=0.0 if world.ended else max(remaining_seconds, 0.0),
        ),
        exchange=_exchange_view(world, viewer),
        speech=[
            SpeechView(
                seq=line.seq,
                time=line.time,
                speaker_id=line.speaker_id,
                text=line.text,
                to=line.to,
                listeners=list(line.listeners),
            )
            for line in visible[-SPEECH_VIEW_LIMIT:]
        ],
        offers=[
            _offer_view(event, event.payload)
            for event in deals
            if isinstance(event.payload, PurchaseOffer | SaleOffer)
        ],
        contract_proposals=[
            _proposal_view(event, event.payload)
            for event in deals
            if isinstance(event.payload, ContractProposal)
        ],
        contracts=[
            ContractView(
                id=contract.id,
                supplier_id=contract.supplier_id,
                buyer_id=contract.buyer_id,
                item=contract.kind,
                quantity=contract.quantity,
                price=contract.price,
                deliveries_left=contract.deliveries_left,
                escrow=contract.escrow,
                deposit=contract.deposit,
                deposit_held=contract.deposit_held,
                remaining_seconds=max(contract.expires_at - world.time, 0.0),
            )
            for contract in (contracts_of(world, viewer) if viewer is not None else ())
        ],
    )


def _offer_view(event: Event, offer: PurchaseOffer | SaleOffer) -> OfferView:
    return OfferView(
        event_id=event.id,
        side="buy" if isinstance(offer, PurchaseOffer) else "sell",
        from_id=event.sender_id,
        to_id=event.recipient_id,
        item=offer.kind,
        quantity=offer.quantity,
        price=offer.price,
    )


def _proposal_view(event: Event, proposal: ContractProposal) -> ContractProposalView:
    supplier_id, buyer_id = proposal.parties(event)
    return ContractProposalView(
        event_id=event.id,
        from_id=event.sender_id,
        to_id=event.recipient_id,
        supplier_id=supplier_id,
        buyer_id=buyer_id,
        item=proposal.kind,
        quantity=proposal.quantity,
        price=proposal.price,
        deliveries=proposal.deliveries,
        duration_seconds=proposal.duration,
        deposit=proposal.deposit,
    )


def _exchange_view(world: World, viewer: str | None) -> ExchangeView:
    def own(fill: Fill) -> Literal["bought", "sold"] | None:
        if viewer == fill.buyer_id:
            return "bought"
        return "sold" if viewer == fill.seller_id else None

    return ExchangeView(
        books=[
            BookView(
                item=item,
                bids=[
                    LevelView(price=price, quantity=quantity)
                    for price, quantity in depth(world, item, "buy", BOOK_LEVELS_SHOWN)
                ],
                asks=[
                    LevelView(price=price, quantity=quantity)
                    for price, quantity in depth(world, item, "sell", BOOK_LEVELS_SHOWN)
                ],
            )
            for item in EXCHANGE_ITEMS
        ],
        fills=[
            FillView(
                seq=fill.seq,
                time=round(fill.time, 1),
                item=fill.item,
                quantity=fill.quantity,
                price=fill.price,
                own=own(fill),
            )
            for fill in world.orders.fills[-FILL_VIEW_LIMIT:]
        ],
        viewer_id=viewer,
        orders=[
            OrderView(
                id=order.id,
                side=order.side,
                item=order.item,
                quantity=order.quantity,
                price=order.price,
                remaining_seconds=max(order.expires_at - world.time, 0.0),
            )
            for order in world.orders.orders
            if viewer is not None and order.resident_id == viewer
        ],
        max_orders=MAX_ORDERS,
    )


def visitors_message(world: World) -> VisitorsMessage | None:
    """The visitors on the map at `world.time`; None when the world has no visitors at all."""
    if not world.crowd.visitors:
        return None
    views = []
    for visitor in world.crowd.visitors:
        here = visitor_position(visitor, world.time)
        if here is None:
            continue
        ahead = remaining_legs(visitor, world.time)
        # The first leg starts where the visitor is, even at the instant it ends one leg and
        # starts the next through a portal.
        legs = [
            LegView(start=_position_view(start), end=_position_view(end)) for start, end in ahead
        ]
        spot = ahead[0][0] if ahead else here
        views.append(VisitorView(id=visitor.id, position=_position_view(spot), legs=legs))
    return VisitorsMessage(speed=VISITOR_SPEED, visitors=views)


def _position_view(position: Position) -> PositionView:
    return PositionView(x=position.x, y=position.y)


def to_action(message: ActionMessage) -> Action:
    action = message.action
    match action:
        case MoveToInput():
            target = Position(action.target.x, action.target.y)
            return MoveTo(resident_id=action.resident_id, target=target)
        case LeaveGameInput():
            return LeaveGame(resident_id=action.resident_id)
        case SayInput():
            return Say(resident_id=action.resident_id, text=action.text, to=action.to)
        case BuyParcelInput():
            return BuyParcel(resident_id=action.resident_id)
        case BuildFacilityInput():
            if action.kind == "ad":
                return BuildAd(action.resident_id, action.parcel_id, action.source)
            if action.kind == "portal":
                return BuildPortal(action.resident_id, action.parcel_id, action.source)
            return BuildVenue(
                action.resident_id,
                action.parcel_id,
                action.kind,
                action.source,
                orientation=action.orientation,
            )
        case MakeContentInput():
            return MakeContent(action.resident_id, action.source, content=action.content)
        case PlaceContentInput():
            return PlaceContent(action.resident_id, action.parcel_id)


def to_llm_settings(message: StartAgentMessage) -> LLMSettings:
    """The settings the player gave, blanks left out."""
    chosen = message.llm
    return LLMSettings(
        base_url=chosen.base_url or None,
        api_key=chosen.api_key or None,
        model=chosen.model or None,
    )


def setting_names(settings: LLMSettings) -> list[LLMSettingName]:
    """The settings given, as the protocol names them."""
    return [_SETTING_NAMES[name] for name in settings.given()]


def encode(message: ServerMessage) -> str:
    # Optional fields are left out rather than sent as null.
    return message.model_dump_json(by_alias=True, exclude_none=True)


def _truncate(detail: str) -> str:
    if len(detail) > ERROR_MAX_LENGTH:
        return detail[: ERROR_MAX_LENGTH - 1] + "…"
    return detail


def bad_message(detail: str) -> ErrorMessage:
    return ErrorMessage(code="bad_message", message=_truncate(detail))


def agent_result(
    resident_id: str, code: AgentRejectionCode | None = None, message: str = ""
) -> AgentResultMessage:
    """Success without a code, a rejection with one."""
    if code is None:
        return AgentResultMessage(resident_id=resident_id, ok=True)
    return AgentResultMessage(
        resident_id=resident_id, ok=False, code=code, message=_truncate(message)
    )


def action_rejected(rejection: ActionRejected) -> ActionResultMessage:
    # The message may echo client input, such as an unknown resident id, so bound it.
    return ActionResultMessage(ok=False, code=rejection.code, message=_truncate(rejection.message))


def parse_client_message(
    text: str,
) -> (
    HelloMessage
    | ViewAsMessage
    | ActionMessage
    | StartAgentMessage
    | StopAgentMessage
    | ErrorMessage
):
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON and over-long integers; deep nesting recurses.
        return bad_message("message is not valid JSON or exceeds parser limits")
    try:
        return _client_message.validate_python(data)
    except ValidationError as error:
        # Without the input: an API key must never come back, even in an error.
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '(root)'}: {issue['msg']}"
            for issue in error.errors(include_input=False)
        )
        return bad_message(issues)
