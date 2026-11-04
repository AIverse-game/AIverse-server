"""Digging shards and refining crystals sent by a page over the socket."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiverse_server.core.world import (
    Inventory,
    Position,
    Quarry,
    Resident,
    Workshop,
    World,
    WorldMap,
)
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.shards import SHARD
from aiverse_server.server.app import create_app
from aiverse_server.server.protocol import PROTOCOL_VERSION

# a stands at the quarry, b at the workshop with the shards and energy for a C1.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(20, 20), Inventory(((ENERGY, 100),))),
        Resident("b", "乙", "#445566", Position(70, 70), Inventory(((ENERGY, 100), (SHARD, 5)))),
    ),
    quarries=(Quarry("q1", Position(20, 20), 4, 300),),
    workshop=Workshop(Position(70, 70), 5),
)


def send(ws: Any, action: dict[str, Any]) -> dict[str, Any]:
    ws.send_json({"type": "action", "action": action})
    return ws.receive_json()  # type: ignore[no-any-return]


def resident(world: dict[str, Any], resident_id: str) -> dict[str, Any]:
    return next(r for r in world["residents"] if r["id"] == resident_id)


def test_a_page_digs_shards_and_refines_a_crystal() -> None:
    now = [0.0]
    with (
        TestClient(create_app(WORLD, clock=lambda: now[0])) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()
        dug = send(ws, {"type": "dig_shards", "residentId": "a", "quarryId": "q1"})
        digging = resident(ws.receive_json()["world"], "a")
        refined = send(ws, {"type": "refine_crystal", "residentId": "b", "tier": "C1"})
        refining = resident(ws.receive_json()["world"], "b")
        now[0] = 15
        ws.send_json({"type": "view_as", "residentId": None})
        done = ws.receive_json()["world"]

    assert (dug["ok"], refined["ok"]) == (True, True)
    assert digging["digging"]["quarryId"] == "q1"
    assert refining["refining"]["tier"] == "C1"
    assert (resident(done, "a")["shards"], resident(done, "b")["crystals"]) == (6, {"C1": 1})
    assert resident(done, "b")["crystalsRefined"] == 1


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ({"type": "dig_shards", "residentId": "b", "quarryId": "q1"}, "not_at_quarry"),
        ({"type": "refine_crystal", "residentId": "a", "tier": "C1"}, "not_at_workshop"),
        ({"type": "refine_crystal", "residentId": "b", "tier": "C2"}, "not_enough_shards"),
    ],
    ids=["dig_away", "refine_away", "refine_short"],
)
def test_dig_and_refine_rejections_reach_the_sender(action: dict[str, Any], code: str) -> None:
    with (
        TestClient(create_app(WORLD, clock=lambda: 0.0)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()

        reply = send(ws, action)

    assert (reply["ok"], reply["code"]) == (False, code)


def test_a_dig_without_a_quarry_id_gets_a_bounded_error() -> None:
    with (
        TestClient(create_app(WORLD, clock=lambda: 0.0)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()

        reply = send(ws, {"type": "dig_shards", "residentId": "a"})

    assert (reply["type"], reply["code"]) == ("error", "bad_message")
