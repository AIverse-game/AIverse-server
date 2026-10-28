"""Wire format shared with the web client (aiverse-client/packages/protocol).

Messages are JSON text frames with camelCase keys. Keep both sides in step and bump
PROTOCOL_VERSION on incompatible changes.
"""

import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from .actions import Action, ActionRejected, LeaveGame, Mine, MoveTo, RejectionCode
from .lifecycle import ranks, total_coins
from .tokens import token_total
from .walking import position_at
from .world import COLOR_PATTERN, Position, World

PROTOCOL_VERSION = 6

ERROR_MAX_LENGTH = 200


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


ActionInput = Annotated[MoveToInput | LeaveGameInput | MineInput, Field(discriminator="type")]


class HelloMessage(_Message):
    type: Literal["hello"]
    protocol_version: StrictInt


class ActionMessage(_Message):
    type: Literal["action"]
    # Only the primitives clients can use so far; answering events and trading come with a
    # way to show them.
    action: ActionInput


ClientMessage = Annotated[HelloMessage | ActionMessage, Field(discriminator="type")]

_client_message: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# Server -> client.


class PositionView(_Message):
    x: float
    y: float


class MiningView(_Message):
    pool_id: str
    amount: int
    """Tokens paid out when the job ends."""
    remaining_seconds: float
    """Time left in the job when this was sent."""


class WalkingView(_Message):
    target: PositionView
    remaining_seconds: float
    """Time left until arrival when this was sent."""


class ResidentView(_Message):
    id: str
    name: str
    color: str = Field(pattern=f"^{COLOR_PATTERN.pattern}$")
    position: PositionView
    """Where the resident is when this was sent, on the way while walking."""
    coins: int
    """Total coins, including those set aside by pending offers; public to everyone."""
    left: bool
    rank: int
    """Place by coins, 1 for the most; equal totals share a place. Final once the game ends."""
    tokens: int
    """Tokens owned, the resident's stamina, including those set aside by pending offers."""
    mining: MiningView | None = None
    """Present while the resident is busy mining."""
    walking: WalkingView | None = None
    """Present while the resident is walking; they head straight for `target` at a steady
    pace and arrive after `remaining_seconds`."""


class PoolView(_Message):
    id: str
    position: PositionView
    radius: float
    reserve: int


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


class WelcomeMessage(_Message):
    type: Literal["welcome"] = "welcome"
    protocol_version: int = PROTOCOL_VERSION


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


ErrorCode = Literal["bad_message", "protocol_mismatch"]


class ErrorMessage(_Message):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str = Field(max_length=ERROR_MAX_LENGTH)


ServerMessage = WelcomeMessage | StateMessage | ActionResultMessage | ErrorMessage


def world_view(world: World, remaining_seconds: float) -> WorldView:
    places = ranks(world)
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
                mining=None
                if resident.mining is None
                else MiningView(
                    pool_id=resident.mining.pool_id,
                    amount=resident.mining.amount,
                    remaining_seconds=max(resident.mining.ends_at - world.time, 0.0),
                ),
                walking=None
                if resident.walking is None
                else WalkingView(
                    target=_position_view(resident.walking.target),
                    remaining_seconds=max(resident.walking.arrives_at - world.time, 0.0),
                ),
            )
            for resident in world.residents
        ],
        game=GameView(
            ended=world.ended,
            remaining_seconds=0.0 if world.ended else max(remaining_seconds, 0.0),
        ),
    )


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


def encode(message: ServerMessage) -> str:
    # Optional fields are left out rather than sent as null.
    return message.model_dump_json(by_alias=True, exclude_none=True)


def _truncate(detail: str) -> str:
    if len(detail) > ERROR_MAX_LENGTH:
        return detail[: ERROR_MAX_LENGTH - 1] + "…"
    return detail


def bad_message(detail: str) -> ErrorMessage:
    return ErrorMessage(code="bad_message", message=_truncate(detail))


def action_rejected(rejection: ActionRejected) -> ActionResultMessage:
    # The message may echo client input, such as an unknown resident id, so bound it.
    return ActionResultMessage(ok=False, code=rejection.code, message=_truncate(rejection.message))


def parse_client_message(text: str) -> HelloMessage | ActionMessage | ErrorMessage:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON and over-long integers; deep nesting recurses.
        return bad_message("message is not valid JSON or exceeds parser limits")
    try:
        return _client_message.validate_python(data)
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '(root)'}: {issue['msg']}"
            for issue in error.errors()
        )
        return bad_message(issues)
