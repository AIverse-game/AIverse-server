import json
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from aiverse_server.core.world import Inventory
from aiverse_server.economy.collectors import COLLECTOR_PRICE
from aiverse_server.game.layout import create_default_world
from aiverse_server.server.app import create_app
from aiverse_server.server.protocol import PROTOCOL_VERSION


def test_collector_actions_flow_through_websocket_and_broadcast_production() -> None:
    initial = create_default_world(7)
    world = replace(
        initial,
        residents=tuple(
            replace(
                r,
                position=initial.springs[0].position,
                inventory=Inventory((("compute", 100), ("v1", 1))),
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

        built = act(
            {"type": "build_collector", "residentId": "r1", "springId": world.springs[0].id}
        )
        installed = act(
            {
                "type": "install_program",
                "residentId": "r2",
                "collectorId": 1,
                "program": "v1",
                "deposit": 500,
            }
        )
        now[0] = 20
        ws.send_json({"type": "view_as", "residentId": "r2"})
        paid = ws.receive_json()["world"]
        removed = act({"type": "remove_program", "residentId": "r2", "collectorId": 1, "slotId": 1})
        # A boolean must never be accepted as a payment amount.
        ws.send_text(
            json.dumps(
                {
                    "type": "action",
                    "action": {
                        "type": "fund_collector",
                        "residentId": "r2",
                        "collectorId": 1,
                        "slotId": 1,
                        "amount": True,
                    },
                }
            )
        )
        refused = ws.receive_json()

    assert built["collectors"][0]["ownerId"] == "r1"
    assert installed["residents"][1]["carriedPrograms"] == {}
    assert installed["residents"][1]["collectorBalance"] == 500
    # Twice a v1's 30 a job, for 30 rent and 12 upkeep, as in the collector unit tests.
    assert paid["residents"][1]["compute"] == 160
    assert paid["residents"][0]["coins"] == 10_000 - COLLECTOR_PRICE + 30
    assert removed["collectors"][0]["slots"] == []
    assert removed["residents"][1]["spendableCoins"] == 9958
    assert refused["type"] == "error" and refused["code"] == "bad_message"
