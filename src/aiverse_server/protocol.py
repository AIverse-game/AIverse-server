"""Wire format shared with the web client (aiverse-client/packages/protocol).

Messages are JSON text frames with camelCase keys. Keep both sides in step and bump
PROTOCOL_VERSION on incompatible changes.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from pydantic.alias_generators import to_camel

from .world import COLOR_PATTERN, World

PROTOCOL_VERSION = 2

ERROR_MAX_LENGTH = 200


class _Message(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


# Client -> server. Untrusted, so validated on arrival; unknown keys are ignored.


class HelloMessage(_Message):
    type: Literal["hello"]
    protocol_version: StrictInt


ClientMessage = HelloMessage


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


ErrorCode = Literal["bad_message", "protocol_mismatch"]


class ErrorMessage(_Message):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str = Field(max_length=ERROR_MAX_LENGTH)


ServerMessage = WelcomeMessage | StateMessage | ErrorMessage


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


def encode(message: ServerMessage) -> str:
    return message.model_dump_json(by_alias=True)


def bad_message(detail: str) -> ErrorMessage:
    if len(detail) > ERROR_MAX_LENGTH:
        detail = detail[: ERROR_MAX_LENGTH - 1] + "…"
    return ErrorMessage(code="bad_message", message=detail)


def parse_client_message(text: str) -> ClientMessage | ErrorMessage:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON and over-long integers; deep nesting recurses.
        return bad_message("message is not valid JSON or exceeds parser limits")
    try:
        return HelloMessage.model_validate(data)
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '(root)'}: {issue['msg']}"
            for issue in error.errors()
        )
        return bad_message(issues)
