"""Gathering data and writing programs sent by a page over the socket."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiverse_server.core.world import (
    Inventory,
    Mine,
    Position,
    Resident,
    Studio,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.gathering import DATA
from aiverse_server.server.app import create_app
from aiverse_server.server.protocol import PROTOCOL_VERSION

# a stands at the mine, b at the studio with the data and compute for a v1.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(20, 20), Inventory(((COMPUTE, 100),))),
        Resident("b", "乙", "#445566", Position(70, 70), Inventory(((COMPUTE, 100), (DATA, 5)))),
    ),
    mines=(Mine("m1", Position(20, 20), 4, 300),),
    studio=Studio(Position(70, 70), 5),
)


def send(ws: Any, action: dict[str, Any]) -> dict[str, Any]:
    ws.send_json({"type": "action", "action": action})
    return ws.receive_json()  # type: ignore[no-any-return]


def resident(world: dict[str, Any], resident_id: str) -> dict[str, Any]:
    return next(r for r in world["residents"] if r["id"] == resident_id)


def test_a_page_gathers_data_and_writes_a_program() -> None:
    now = [0.0]
    with (
        TestClient(create_app(WORLD, clock=lambda: now[0])) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()
        gathered = send(ws, {"type": "gather_data", "residentId": "a", "mineId": "m1"})
        gathering = resident(ws.receive_json()["world"], "a")
        written = send(ws, {"type": "write_program", "residentId": "b", "tier": "v1"})
        writing = resident(ws.receive_json()["world"], "b")
        now[0] = 15
        ws.send_json({"type": "view_as", "residentId": None})
        done = ws.receive_json()["world"]

    assert (gathered["ok"], written["ok"]) == (True, True)
    assert gathering["gathering"]["mineId"] == "m1"
    assert writing["writing"]["tier"] == "v1"
    assert (resident(done, "a")["data"], resident(done, "b")["programs"]) == (6, {"v1": 1})
    assert resident(done, "b")["programsWritten"] == 1


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ({"type": "gather_data", "residentId": "b", "mineId": "m1"}, "not_at_mine"),
        ({"type": "write_program", "residentId": "a", "tier": "v1"}, "not_at_studio"),
        ({"type": "write_program", "residentId": "b", "tier": "v2"}, "not_enough_data"),
    ],
    ids=["gather_away", "write_away", "write_short"],
)
def test_gather_and_write_rejections_reach_the_sender(action: dict[str, Any], code: str) -> None:
    with (
        TestClient(create_app(WORLD, clock=lambda: 0.0)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()

        reply = send(ws, action)

    assert (reply["ok"], reply["code"]) == (False, code)


def test_a_gather_without_a_mine_id_gets_a_bounded_error() -> None:
    with (
        TestClient(create_app(WORLD, clock=lambda: 0.0)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()

        reply = send(ws, {"type": "gather_data", "residentId": "a"})

    assert (reply["type"], reply["code"]) == ("error", "bad_message")
