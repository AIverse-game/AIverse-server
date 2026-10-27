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

from .actions import Action, ActionRejected, MoveTo, RejectionCode
from .world import COLOR_PATTERN, Position, World

PROTOCOL_VERSION = 3

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


class HelloMessage(_Message):
    type: Literal["hello"]
    protocol_version: StrictInt


class ActionMessage(_Message):
    type: Literal["action"]
    # Only move_to is on the wire so far; becomes a union discriminated on `type` as more
    # primitives reach clients.
    action: MoveToInput


ClientMessage = Annotated[HelloMessage | ActionMessage, Field(discriminator="type")]

_client_message: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# Server -> client.


class PositionView(_Message):
    x: float
    y: float


class ResidentView(_Message):
    id: str
    name: str
    color: str = Field(pattern=f"^{COLOR_PATTERN.pattern}$")
    position: PositionView


class MapView(_Message):
    size: float


class WorldView(_Message):
    map: MapView
    residents: list[ResidentView]


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


def world_view(world: World) -> WorldView:
    return WorldView(
        map=MapView(size=world.map.size),
        residents=[
            ResidentView(
                id=resident.id,
                name=resident.name,
                color=resident.color,
                position=PositionView(x=resident.position.x, y=resident.position.y),
            )
            for resident in world.residents
        ],
    )


def to_action(message: ActionMessage) -> Action:
    action = message.action
    return MoveTo(resident_id=action.resident_id, target=Position(action.target.x, action.target.y))


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
