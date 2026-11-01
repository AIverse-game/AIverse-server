import json
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from aiverse_server.core.world import Inventory
from aiverse_server.economy.rigs import RIG_PRICE
from aiverse_server.game.layout import create_default_world
from aiverse_server.server.app import create_app
from aiverse_server.server.protocol import PROTOCOL_VERSION


def test_rig_actions_flow_through_websocket_and_broadcast_production() -> None:
    initial = create_default_world(7)
    world = replace(
        initial,
        residents=tuple(
            replace(
                r,
                position=initial.pools[0].position,
                inventory=Inventory((("token", 100), ("MK1", 1))),
            )
            for r in initial.residents
        ),
    )
    now = [0.0]
    with (
        TestClient(create_app(world, clock=lambda: now[0])) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "hello", "protocolVersion": PROTOCOL_VERSION})
        ws.receive_json()
        ws.receive_json()

        def act(action: dict[str, Any]) -> dict[str, Any]:
            ws.send_json({"type": "action", "action": action})
            assert ws.receive_json() == {"type": "action_result", "ok": True}
            return ws.receive_json()["world"]  # type: ignore[no-any-return]

        built = act({"type": "build_rig", "residentId": "r1", "poolId": world.pools[0].id})
        installed = act(
            {"type": "install_chip", "residentId": "r2", "rigId": 1, "chip": "MK1", "deposit": 500}
        )
        now[0] = 20
        ws.send_json({"type": "view_as", "residentId": "r2"})
        paid = ws.receive_json()["world"]
        removed = act({"type": "remove_chip", "residentId": "r2", "rigId": 1, "slotId": 1})
        # A boolean must never be accepted as a payment amount.
        ws.send_text(
            json.dumps(
                {
                    "type": "action",
                    "action": {
                        "type": "fund_rig",
                        "residentId": "r2",
                        "rigId": 1,
                        "slotId": 1,
                        "amount": True,
                    },
                }
            )
        )
        refused = ws.receive_json()

    assert built["rigs"][0]["ownerId"] == "r1"
    assert installed["residents"][1]["carriedChips"] == {}
    assert installed["residents"][1]["rigBalance"] == 500
    # Twice an MK1's 30 a job, for 30 rent and 12 electricity, as in the rig unit tests.
    assert paid["residents"][1]["tokens"] == 160
    assert paid["residents"][0]["coins"] == 10_000 - RIG_PRICE + 30
    assert removed["rigs"][0]["slots"] == []
    assert removed["residents"][1]["spendableCoins"] == 9958
    assert refused["type"] == "error" and refused["code"] == "bad_message"
