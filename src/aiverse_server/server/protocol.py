"""Wire format shared with the web client (aiverse-client/packages/protocol).

Messages are JSON text frames with camelCase keys. Keep both sides in step and bump
PROTOCOL_VERSION on incompatible changes.
"""

import json
from collections.abc import Callable, Mapping, Sequence
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
from ..core.world import COLOR_PATTERN, Position, World
from ..economy.contracts import ContractProposal, contracts_of
from ..economy.crystals import CRYSTAL_TIERS
from ..economy.energy import energy_total
from ..economy.market import (
    MARKET_BASE_PRICE,
    MARKET_DEPTH,
    MARKET_HALF_LIFE,
    MARKET_RESTING_PRESSURE,
    pressure_at,
)
from ..economy.towers import (
    RENT_PER_100,
    TOWER_CAPACITY,
    TOWER_PRICE,
    TOWER_SECONDS,
    TOWER_YIELD,
    UPKEEP_PER_100,
    cycle_amount,
    cycle_cost,
    slot_status,
    tower_coins,
)
from ..economy.trade import PurchaseOffer, SaleOffer, reserved_items
from ..game.actions import (
    Action,
    ActionRejected,
    BuildTower,
    BuyCrystal,
    FundTower,
    Harvest,
    InstallCrystal,
    LeaveGame,
    MoveTo,
    RejectionCode,
    RemoveCrystal,
    Say,
    SellEnergy,
)
from ..game.lifecycle import GameErrorCode, ranks, total_coins
from ..residents.walking import position_at

PROTOCOL_VERSION = 18

ERROR_MAX_LENGTH = 200

SPEECH_VIEW_LIMIT = 50
"""Latest lines of speech each state carries, so a long game does not grow every state."""


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


class HarvestInput(_Message):
    type: Literal["harvest"]
    resident_id: str
    spring_id: str


class SellEnergyInput(_Message):
    type: Literal["sell_energy"]
    resident_id: str
    quantity: StrictInt


class BuyCrystalInput(_Message):
    type: Literal["buy_crystal"]
    resident_id: str
    tier: str


class SayInput(_Message):
    type: Literal["say"]
    resident_id: str
    text: str
    to: str | None = None
    """The one resident spoken to; left out to speak aloud to everyone within earshot."""


class BuildTowerInput(_Message):
    type: Literal["build_tower"]
    resident_id: str
    spring_id: str


class InstallCrystalInput(_Message):
    type: Literal["install_crystal"]
    resident_id: str
    tower_id: StrictInt
    crystal: str
    deposit: StrictInt


class FundTowerInput(_Message):
    type: Literal["fund_tower"]
    resident_id: str
    tower_id: StrictInt
    slot_id: StrictInt
    amount: StrictInt


class RemoveCrystalInput(_Message):
    type: Literal["remove_crystal"]
    resident_id: str
    tower_id: StrictInt
    slot_id: StrictInt


ActionInput = Annotated[
    MoveToInput
    | LeaveGameInput
    | HarvestInput
    | SellEnergyInput
    | BuyCrystalInput
    | SayInput
    | BuildTowerInput
    | InstallCrystalInput
    | FundTowerInput
    | RemoveCrystalInput,
    Field(discriminator="type"),
]


class HelloMessage(_Message):
    type: Literal["hello"]
    protocol_version: StrictInt


class ViewAsMessage(_Message):
    """Choose whose whispers, offers and contracts this connection sees: the resident's, or
    none for None. Pages send it when the player picks a resident; there is no ownership yet,
    so any resident may be chosen."""

    type: Literal["view_as"]
    resident_id: str | None


class ActionMessage(_Message):
    type: Literal["action"]
    # Only the primitives clients can use so far; pages see offers and contracts now, but
    # making and answering them is still left to agents.
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


class HarvestView(_Message):
    spring_id: str
    amount: int
    """Energy paid out when the job ends."""
    crystal: str
    """Tier of the crystal doing the work."""
    remaining_seconds: float
    """Time left in the job when this was sent."""


class WalkingView(_Message):
    target: PositionView
    remaining_seconds: float
    """Time left until arrival when this was sent."""


class AgentView(_Message):
    """Present on a resident an agent drives."""

    model: str | None = None
    """The language model asking, as the server calls it; left out for a scripted brain."""


class ResidentView(_Message):
    id: str
    name: str
    color: str = Field(pattern=f"^{COLOR_PATTERN.pattern}$")
    position: PositionView
    """Where the resident is when this was sent, on the way while walking."""
    coins: int
    spendable_coins: int
    tower_balance: int
    """Total coins, including those set aside by pending offers and contracts; public to
    everyone."""
    left: bool
    rank: int
    """Place by coins, 1 for the most; equal totals share a place. Final once the game ends."""
    energy: int
    """Energy owned, the resident's stamina, including those set aside by pending offers."""
    crystals: dict[str, int]
    carried_crystals: dict[str, int]
    """Crystals owned, tier to count, including those set aside by pending offers;
    tiers with none are left out."""
    harvesting: HarvestView | None = None
    """Present while the resident is busy harvesting."""
    walking: WalkingView | None = None
    """Present while the resident is walking; they head straight for `target` at a steady
    pace and arrive after `remaining_seconds`."""
    agent: AgentView | None = None
    """Present while an agent drives the resident rather than the pages."""


class SpringView(_Message):
    id: str
    position: PositionView
    radius: float
    reserve: int


class CrystalTierView(_Message):
    tier: str
    price: int
    """Coins one crystal costs at the market."""
    energy_per_job: int
    """Energy one harvesting job draws while the spring has that much left."""


class PricePointView(_Message):
    time: float
    """Game seconds since the start."""
    price: float
    """Coins one more energy fetched then, before rounding."""


class MarketView(_Message):
    """The market and what sets its price, so clients can follow the price as it moves
    between states: one more energy fetches `base_price * depth / (depth + p)` coins, where
    the distance of the pressure p from `resting_pressure` halves every `half_life_seconds`
    after this was sent, and selling q energy pays
    `floor(base_price * depth * ln((depth + p + q) / (depth + p)))`."""

    position: PositionView
    radius: float
    base_price: float
    depth: float
    half_life_seconds: float
    resting_pressure: float
    """Pressure the market drifts to with no sales; below zero, so the price then climbs
    above `base_price`."""
    pressure: float
    """Recent sales in energy when this was sent; below zero after a quiet while."""
    crystals: list[CrystalTierView]
    """Crystals on sale here at fixed prices, worst first."""
    price_history: list[PricePointView]
    """The energy price over the game so far, oldest first: noted at every sale and every few
    seconds in between, so a market nobody sells to still shows its price climbing."""


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


class GameView(_Message):
    ended: bool
    remaining_seconds: float
    """Time left when this was sent; 0 once the game has ended."""


class ResourceBalanceView(_Message):
    coins: int
    energy: int


class AgentCallView(_Message):
    name: str
    arguments: str
    time: float
    ok: bool
    code: str | None = None
    coins_change: int
    energy_change: int


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
        before=ResourceBalanceView(coins=turn.before.coins, energy=turn.before.energy),
        after=ResourceBalanceView(coins=turn.after.coins, energy=turn.after.energy),
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
                energy_change=call.energy_change,
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


class TowerSlotView(_Message):
    id: int
    resident_id: str
    crystal: str
    balance: int
    harvested: int
    rent_paid: int
    status: Literal["running", "no_funds", "spring_empty"]
    remaining_seconds: float
    energy_per_cycle: int
    rent_per_cycle: int
    upkeep_per_cycle: int


class TowerView(_Message):
    id: int
    owner_id: str
    spring_id: str
    slots: list[TowerSlotView]


class TowerRulesView(_Message):
    price: int = TOWER_PRICE
    capacity: int = TOWER_CAPACITY
    cycle_seconds: float = TOWER_SECONDS
    yield_multiplier: int = TOWER_YIELD
    """A crystal in a tower harvests this many times its energy per job each cycle."""
    rent_per_100: int = RENT_PER_100
    upkeep_per_100: int = UPKEEP_PER_100


def tower_views(world: World) -> list[TowerView]:
    return [
        TowerView(
            id=tower.id,
            owner_id=tower.owner_id,
            spring_id=tower.spring_id,
            slots=[
                TowerSlotView(
                    id=slot.id,
                    resident_id=slot.resident_id,
                    crystal=slot.crystal,
                    balance=slot.balance,
                    harvested=slot.harvested,
                    rent_paid=slot.rent_paid,
                    status=slot_status(world, tower, slot),
                    remaining_seconds=max(slot.next_at - world.time, 0.0),
                    energy_per_cycle=cycle_amount(world, tower, slot),
                    rent_per_cycle=cycle_cost(tower, slot, cycle_amount(world, tower, slot))[0],
                    upkeep_per_cycle=cycle_cost(tower, slot, cycle_amount(world, tower, slot))[1],
                )
                for slot in tower.slots
            ],
        )
        for tower in world.towers
    ]


class WorldView(_Message):
    map: MapView
    springs: list[SpringView]
    residents: list[ResidentView]
    game: GameView
    towers: list[TowerView]
    tower_rules: TowerRulesView
    market: MarketView | None = None
    """Left out on a map without a market."""
    speech: list[SpeechView]
    """The latest `SPEECH_VIEW_LIMIT` lines the connection may see, oldest first: every line
    spoken aloud, and the whispers to or from the resident it views as."""
    offers: list[OfferView]
    """Trade offers to or from the resident the connection views as, oldest first; none
    when it views as nobody. Others' deals are private, like whispers."""
    contract_proposals: list[ContractProposalView]
    """Contract proposals to or from the viewed resident, oldest first."""
    contracts: list[ContractView]
    agent_timeline: AgentTimelineView | None = None
    """Contracts in force the viewed resident is a party to, oldest first."""


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
    WelcomeMessage | StateMessage | ActionResultMessage | AgentResultMessage | ErrorMessage
)


def world_view(
    world: World,
    remaining_seconds: float,
    viewer: str | None = None,
    agents: Mapping[str, str | None] | None = None,
    agent_turns: Mapping[str, Sequence[AgentTurnView]] | None = None,
) -> WorldView:
    """The world as a connection viewing as resident `viewer`, or as nobody, sees it: the
    same for everyone except whispers, offers and contracts, of which it sees only the
    viewer's. `agents` maps
    the residents agents drive to the model each asks, None for a scripted brain."""
    driven = agents or {}
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
    return WorldView(
        towers=tower_views(world),
        tower_rules=TowerRulesView(),
        agent_timeline=AgentTimelineView(
            resident_id=viewer, turns=list((agent_turns or {}).get(viewer, ()))
        )
        if viewer is not None
        else None,
        map=MapView(size=world.map.size),
        springs=[
            SpringView(
                id=spring.id,
                position=_position_view(spring.position),
                radius=spring.radius,
                reserve=spring.reserve,
            )
            for spring in world.springs
        ],
        residents=[
            ResidentView(
                id=resident.id,
                name=resident.name,
                color=resident.color,
                position=_position_view(position_at(resident, world.time)),
                coins=total_coins(world, resident.id),
                spendable_coins=resident.coins,
                tower_balance=tower_coins(world, resident.id),
                carried_crystals={
                    m.tier: resident.inventory.count(m.tier)
                    for m in CRYSTAL_TIERS
                    if resident.inventory.count(m.tier)
                },
                left=resident.left,
                rank=places[resident.id],
                energy=energy_total(world, resident.id),
                crystals=_crystals_owned(world, resident.id),
                harvesting=None
                if resident.harvesting is None
                else HarvestView(
                    spring_id=resident.harvesting.spring_id,
                    amount=resident.harvesting.amount,
                    crystal=resident.harvesting.crystal,
                    remaining_seconds=max(resident.harvesting.ends_at - world.time, 0.0),
                ),
                walking=None
                if resident.walking is None
                else WalkingView(
                    target=_position_view(resident.walking.target),
                    remaining_seconds=max(resident.walking.arrives_at - world.time, 0.0),
                ),
                agent=AgentView(model=driven[resident.id]) if resident.id in driven else None,
            )
            for resident in world.residents
        ],
        game=GameView(
            ended=world.ended,
            remaining_seconds=0.0 if world.ended else max(remaining_seconds, 0.0),
        ),
        market=None
        if world.market is None
        else MarketView(
            position=_position_view(world.market.position),
            radius=world.market.radius,
            base_price=MARKET_BASE_PRICE,
            depth=MARKET_DEPTH,
            half_life_seconds=MARKET_HALF_LIFE,
            resting_pressure=MARKET_RESTING_PRESSURE,
            pressure=pressure_at(world.market, world.time),
            crystals=[
                CrystalTierView(
                    tier=tier.tier, price=tier.price, energy_per_job=tier.energy_per_job
                )
                for tier in CRYSTAL_TIERS
            ],
            price_history=[
                PricePointView(time=point.time, price=point.price) for point in world.market.history
            ],
        ),
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


def _crystals_owned(world: World, resident_id: str) -> dict[str, int]:
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    owned = {
        tier.tier: resident.inventory.count(tier.tier)
        + reserved_items(world, resident_id, tier.tier)
        for tier in CRYSTAL_TIERS
    }
    return {tier: count for tier, count in owned.items() if count > 0}


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
        case HarvestInput():
            return Harvest(resident_id=action.resident_id, spring_id=action.spring_id)
        case SellEnergyInput():
            return SellEnergy(resident_id=action.resident_id, quantity=action.quantity)
        case BuyCrystalInput():
            return BuyCrystal(resident_id=action.resident_id, tier=action.tier)
        case BuildTowerInput():
            return BuildTower(action.resident_id, action.spring_id)
        case InstallCrystalInput():
            return InstallCrystal(
                action.resident_id, action.tower_id, action.crystal, action.deposit
            )
        case FundTowerInput():
            return FundTower(action.resident_id, action.tower_id, action.slot_id, action.amount)
        case RemoveCrystalInput():
            return RemoveCrystal(action.resident_id, action.tower_id, action.slot_id)
        case SayInput():
            return Say(resident_id=action.resident_id, text=action.text, to=action.to)


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
