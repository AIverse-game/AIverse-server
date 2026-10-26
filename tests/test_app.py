import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiverse_server.app import create_app
from aiverse_server.protocol import ERROR_MAX_LENGTH, PROTOCOL_VERSION, bad_message
from aiverse_server.world import Position, Resident, World, WorldMap

HELLO = {"type": "hello", "protocolVersion": PROTOCOL_VERSION}

SMALL_WORLD = World(
    map=WorldMap(size=50),
    residents=(
        Resident(id="a", name="甲", color="#112233", position=Position(0, 50)),
        Resident(id="b", name="乙", color="#AABBCC", position=Position(12.5, 7)),
    ),
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(SMALL_WORLD))


def test_hello_returns_welcome_then_the_map(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(HELLO))
        assert ws.receive_json() == {"type": "welcome", "protocolVersion": PROTOCOL_VERSION}
        assert ws.receive_json() == {
            "type": "state",
            "world": {
                "map": {"size": 50},
                "residents": [
                    {"id": "a", "name": "甲", "color": "#112233", "position": {"x": 0, "y": 50}},
                    {"id": "b", "name": "乙", "color": "#AABBCC", "position": {"x": 12.5, "y": 7}},
                ],
            },
        }


def test_default_app_serves_the_fixed_layout() -> None:
    with TestClient(create_app()).websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(HELLO))
        ws.receive_json()
        world = ws.receive_json()["world"]
    assert world["map"]["size"] == 100
    assert [r["id"] for r in world["residents"]] == ["r1", "r2", "r3", "r4"]


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        json.dumps(None),
        json.dumps({"type": "start"}),
        json.dumps({"type": "hello"}),
        json.dumps({"type": "hello", "protocolVersion": str(PROTOCOL_VERSION)}),
        json.dumps({"type": "hello", "protocolVersion": True}),
        json.dumps({"type": "x" * 1000}),
        "[" * 100_000,
        '{"type": "hello", "protocolVersion": ' + "9" * 5000 + "}",
    ],
)
def test_invalid_messages_get_a_bounded_error(client: TestClient, frame: str) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(frame)
        reply: dict[str, Any] = ws.receive_json()
    assert reply["type"] == "error"
    assert reply["code"] == "bad_message"
    assert 0 < len(reply["message"]) <= ERROR_MAX_LENGTH


def test_socket_stays_usable_after_hostile_json(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text("[" * 100_000)
        assert ws.receive_json()["code"] == "bad_message"
        ws.send_text(json.dumps(HELLO))
        assert ws.receive_json()["type"] == "welcome"


def test_unknown_keys_are_ignored(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({**HELLO, "extra": 1}))
        assert ws.receive_json()["type"] == "welcome"


def test_wrong_protocol_version_is_rejected_and_the_socket_stays_usable(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "hello", "protocolVersion": PROTOCOL_VERSION + 1}))
        assert ws.receive_json()["code"] == "protocol_mismatch"
        ws.send_bytes(b"\x00")
        assert ws.receive_json()["code"] == "bad_message"
        ws.send_text(json.dumps(HELLO))
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["type"] == "state"


def test_error_details_are_truncated() -> None:
    message = bad_message("x" * 500).message
    assert len(message) == ERROR_MAX_LENGTH
    assert message.endswith("…")
