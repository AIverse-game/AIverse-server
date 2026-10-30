"""Wire format shared with the web client (aiverse-client/packages/protocol).

Messages are JSON text frames with camelCase keys. Keep both sides in step and bump
PROTOCOL_VERSION on incompatible changes.
"""

import json
from collections.abc import Mapping
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
from ..core.world import COLOR_PATTERN, Position, World
from ..economy.chips import CHIP_MODELS
from ..economy.market import MARKET_BASE_PRICE, MARKET_DEPTH, MARKET_HALF_LIFE, pressure_at
from ..economy.tokens import token_total
from ..economy.trade import reserved_items
from ..game.actions import (
    Action,
    ActionRejected,
    BuyChip,
    LeaveGame,
    Mine,
    MoveTo,
    RejectionCode,
    Say,
    SellTokens,
)
from ..game.lifecycle import GameErrorCode, ranks, total_coins
from ..residents.walking import position_at

PROTOCOL_VERSION = 11

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


class MineInput(_Message):
    type: Literal["mine"]
    resident_id: str
    pool_id: str


class SellTokensInput(_Message):
    type: Literal["sell_tokens"]
    resident_id: str
    quantity: StrictInt


class BuyChipInput(_Message):
    type: Literal["buy_chip"]
    resident_id: str
    model: str


class SayInput(_Message):
    type: Literal["say"]
    resident_id: str
    text: str
    to: str | None = None
    """The one resident spoken to; left out to speak aloud to everyone within earshot."""


ActionInput = Annotated[
    MoveToInput | LeaveGameInput | MineInput | SellTokensInput | BuyChipInput | SayInput,
    Field(discriminator="type"),
]


class HelloMessage(_Message):
    type: Literal["hello"]
    protocol_version: StrictInt


class ViewAsMessage(_Message):
    """Choose whose whispers this connection sees: the resident's, or none for None. Pages
    send it when the player picks a resident; there is no ownership yet, so any resident
    may be chosen."""

    type: Literal["view_as"]
    resident_id: str | None


class ActionMessage(_Message):
    type: Literal["action"]
    # Only the primitives clients can use so far; answering events and trading come with a
    # way to show them.
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


class MiningView(_Message):
    pool_id: str
    amount: int
    """Tokens paid out when the job ends."""
    chip: str
    """Model of the chip doing the work."""
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
    """Total coins, including those set aside by pending offers and contracts; public to
    everyone."""
    left: bool
    rank: int
    """Place by coins, 1 for the most; equal totals share a place. Final once the game ends."""
    tokens: int
    """Tokens owned, the resident's stamina, including those set aside by pending offers."""
    chips: dict[str, int]
    """Chips owned, model to count, including those set aside by pending offers;
    models with none are left out."""
    mining: MiningView | None = None
    """Present while the resident is busy mining."""
    walking: WalkingView | None = None
    """Present while the resident is walking; they head straight for `target` at a steady
    pace and arrive after `remaining_seconds`."""
    agent: AgentView | None = None
    """Present while an agent drives the resident rather than the pages."""


class PoolView(_Message):
    id: str
    position: PositionView
    radius: float
    reserve: int


class ChipModelView(_Message):
    model: str
    price: int
    """Coins one chip costs at the market."""
    tokens_per_job: int
    """Tokens one mining job digs out while the pool has that many left."""


class MarketView(_Message):
    """The market and what sets its price, so clients can follow the price as it recovers
    between states: one more token fetches `base_price * depth / (depth + p)` coins, where
    the pressure p halves every `half_life_seconds` after this was sent, and selling q tokens
    pays `floor(base_price * depth * ln((depth + p + q) / (depth + p)))`."""

    position: PositionView
    radius: float
    base_price: float
    depth: float
    half_life_seconds: float
    pressure: float
    """Recent sales in tokens when this was sent."""
    chips: list[ChipModelView]
    """Chips on sale here at fixed prices, worst first."""


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


class MapView(_Message):
    size: float


class GameView(_Message):
    ended: bool
    remaining_seconds: float
    """Time left when this was sent; 0 once the game has ended."""


class WorldView(_Message):
    map: MapView
    pools: list[PoolView]
    residents: list[ResidentView]
    game: GameView
    market: MarketView | None = None
    """Left out on a map without a market."""
    speech: list[SpeechView]
    """The latest `SPEECH_VIEW_LIMIT` lines the connection may see, oldest first: every line
    spoken aloud, and the whispers to or from the resident it views as."""


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
) -> WorldView:
    """The world as a connection viewing as resident `viewer`, or as nobody, sees it: the
    same for everyone except the whispers, of which it sees only the viewer's. `agents` maps
    the residents agents drive to the model each asks, None for a scripted brain."""
    driven = agents or {}
    places = ranks(world)
    visible = [
        line
        for line in world.speech.entries
        if line.to is None or (viewer is not None and viewer in (line.speaker_id, line.to))
    ]
    return WorldView(
        map=MapView(size=world.map.size),
        pools=[
            PoolView(
                id=pool.id,
                position=_position_view(pool.position),
                radius=pool.radius,
                reserve=pool.reserve,
            )
            for pool in world.pools
        ],
        residents=[
            ResidentView(
                id=resident.id,
                name=resident.name,
                color=resident.color,
                position=_position_view(position_at(resident, world.time)),
                coins=total_coins(world, resident.id),
                left=resident.left,
                rank=places[resident.id],
                tokens=token_total(world, resident.id),
                chips=_chips_owned(world, resident.id),
                mining=None
                if resident.mining is None
                else MiningView(
                    pool_id=resident.mining.pool_id,
                    amount=resident.mining.amount,
                    chip=resident.mining.chip,
                    remaining_seconds=max(resident.mining.ends_at - world.time, 0.0),
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
            pressure=pressure_at(world.market, world.time),
            chips=[
                ChipModelView(
                    model=model.model, price=model.price, tokens_per_job=model.tokens_per_job
                )
                for model in CHIP_MODELS
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
    )


def _chips_owned(world: World, resident_id: str) -> dict[str, int]:
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    owned = {
        model.model: resident.inventory.count(model.model)
        + reserved_items(world, resident_id, model.model)
        for model in CHIP_MODELS
    }
    return {model: count for model, count in owned.items() if count > 0}


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
        case MineInput():
            return Mine(resident_id=action.resident_id, pool_id=action.pool_id)
        case SellTokensInput():
            return SellTokens(resident_id=action.resident_id, quantity=action.quantity)
        case BuyChipInput():
            return BuyChip(resident_id=action.resident_id, model=action.model)
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
