import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aiverse_server.core.world import (
    Inventory,
    Market,
    MiningJob,
    Pool,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.chips import CHIP_MODELS
from aiverse_server.economy.market import MARKET_BASE_PRICE, MARKET_DEPTH, MARKET_HALF_LIFE
from aiverse_server.economy.mining import MINE_SECONDS
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import OfferToSell, ProposeContract, RespondToEvent, Say, perform
from aiverse_server.game.layout import POOL_COUNT, STARTING_TOKENS
from aiverse_server.game.lifecycle import GAME_DURATION
from aiverse_server.residents.walking import WALK_SPEED
from aiverse_server.server.app import CLOSE_POLICY_VIOLATION, Game, Outbox, create_app
from aiverse_server.server.protocol import (
    ERROR_MAX_LENGTH,
    PROTOCOL_VERSION,
    SPEECH_VIEW_LIMIT,
    bad_message,
    world_view,
)

HELLO = {"type": "hello", "protocolVersion": PROTOCOL_VERSION}

TOKENS = Inventory(((TOKEN, 100), ("MK1", 1)))

MINE_YIELD = CHIP_MODELS[0].tokens_per_job
"""What a job pays with MK1, the chip both residents carry."""

SMALL_WORLD = World(
    map=WorldMap(size=50),
    residents=(
        Resident(id="a", name="甲", color="#112233", position=Position(0, 50), inventory=TOKENS),
        Resident(id="b", name="乙", color="#AABBCC", position=Position(12.5, 7), inventory=TOKENS),
    ),
    pools=(Pool(id="p1", position=Position(10, 10), radius=4, reserve=40),),
)


class FakeClock:
    """Time that moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    return TestClient(create_app(SMALL_WORLD, clock=clock))


def test_hello_returns_welcome_then_the_map(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(HELLO))
        assert ws.receive_json() == {
            "type": "welcome",
            "protocolVersion": PROTOCOL_VERSION,
            "llmOverrides": [],
        }
        assert ws.receive_json() == {
            "type": "state",
            "world": {
                "map": {"size": 50},
                "pools": [{"id": "p1", "position": {"x": 10, "y": 10}, "radius": 4, "reserve": 40}],
                "residents": [
                    {
                        "id": "a",
                        "name": "甲",
                        "color": "#112233",
                        "position": {"x": 0, "y": 50},
                        "coins": 0,
                        "left": False,
                        "rank": 1,
                        "tokens": 100,
                        "chips": {"MK1": 1},
                    },
                    {
                        "id": "b",
                        "name": "乙",
                        "color": "#AABBCC",
                        "position": {"x": 12.5, "y": 7},
                        "coins": 0,
                        "left": False,
                        "rank": 1,
                        "tokens": 100,
                        "chips": {"MK1": 1},
                    },
                ],
                "game": {"ended": False, "remainingSeconds": GAME_DURATION},
                "speech": [],
                "offers": [],
                "contractProposals": [],
                "contracts": [],
            },
        }


def test_default_app_serves_the_fixed_layout() -> None:
    with TestClient(create_app()).websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(HELLO))
        ws.receive_json()
        world = ws.receive_json()["world"]
    assert world["map"]["size"] == 100
    assert [r["id"] for r in world["residents"]] == ["r1", "r2", "r3", "r4"]
    assert {r["coins"] for r in world["residents"]} == {10_000}


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


def move(resident_id: str, x: object, y: object) -> str:
    action = {"type": "move_to", "residentId": resident_id, "target": {"x": x, "y": y}}
    return json.dumps({"type": "action", "action": action})


def greet(ws: Any) -> dict[str, Any]:
    """Says hello and returns the world from the reply."""
    ws.send_text(json.dumps(HELLO))
    assert ws.receive_json()["type"] == "welcome"
    state: dict[str, Any] = ws.receive_json()
    assert state["type"] == "state"
    return state["world"]  # type: ignore[no-any-return]


def positions(world: dict[str, Any]) -> dict[str, dict[str, float]]:
    return {r["id"]: r["position"] for r in world["residents"]}


def test_move_to_is_acknowledged_then_the_walk_and_the_arrival_are_sent(
    client: TestClient, clock: FakeClock
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(move("b", 32.5, 7))
        assert ws.receive_json() == {"type": "action_result", "ok": True}
        state = ws.receive_json()
        assert state["type"] == "state"
        b = resident_view(state["world"], "b")
        assert b["position"] == {"x": 12.5, "y": 7}
        assert b["walking"] == {"target": {"x": 32.5, "y": 7}, "remainingSeconds": 20 / WALK_SPEED}
        # Halfway there, the next state shows b on the way.
        clock.now = 10 / WALK_SPEED
        ws.send_text(move("a", 0, 49))
        assert ws.receive_json()["ok"] is True
        assert resident_view(ws.receive_json()["world"], "b")["position"] == {"x": 22.5, "y": 7}
        clock.now = 20 / WALK_SPEED
        ws.send_text(move("b", 32.5, 7))
        # The frame first brings b in and tells everyone, then b, no longer busy, may act.
        assert ws.receive_json()["ok"] is True
        b = resident_view(ws.receive_json()["world"], "b")
    assert b["position"] == {"x": 32.5, "y": 7}
    assert "walking" not in b


def test_moves_reach_every_greeted_connection_and_persist(
    client: TestClient, clock: FakeClock
) -> None:
    heading = {"x": 5, "y": 6}
    with client.websocket_connect("/ws") as mover, client.websocket_connect("/ws") as watcher:
        greet(mover)
        greet(watcher)
        with client.websocket_connect("/ws") as stranger:
            mover.send_text(move("a", 5, 6))
            assert mover.receive_json()["ok"] is True
            assert resident_view(mover.receive_json()["world"], "a")["walking"]["target"] == heading
            assert (
                resident_view(watcher.receive_json()["world"], "a")["walking"]["target"] == heading
            )
            # Not greeted, so no broadcast was queued: the welcome comes first.
            assert resident_view(greet(stranger), "a")["walking"]["target"] == heading
    clock.now = 100
    with client.websocket_connect("/ws") as later:
        assert positions(greet(later))["a"] == heading


def test_actions_before_hello_are_refused(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(move("a", 5, 5))
        reply = ws.receive_json()
        assert reply["code"] == "bad_message"
        assert "hello" in reply["message"]
        assert positions(greet(ws))["a"] == {"x": 0, "y": 50}


@pytest.mark.parametrize(
    ("resident_id", "x", "y", "code"),
    [
        ("a", 50.01, 0, "out_of_bounds"),
        ("a", 0, -1, "out_of_bounds"),
        ("nobody", 1, 1, "unknown_resident"),
        ("x" * 1000, 1, 1, "unknown_resident"),
    ],
)
def test_rejected_actions_change_nothing(
    client: TestClient, resident_id: str, x: float, y: float, code: str
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(move(resident_id, x, y))
        reply = ws.receive_json()
        assert reply["type"] == "action_result"
        assert reply["ok"] is False
        assert reply["code"] == code
        assert 0 < len(reply["message"]) <= ERROR_MAX_LENGTH
        # No state follows a rejection; the next message answers the next hello.
        assert positions(greet(ws)) == {"a": {"x": 0, "y": 50}, "b": {"x": 12.5, "y": 7}}


@pytest.mark.parametrize(
    "frame",
    [
        move("a", True, 1),
        move("a", "1", 1),
        move("a", None, 1),
        move("a", 1, 1).replace("1", "NaN", 1),
        move("a", 1, 1).replace("1", "Infinity", 1),
        json.dumps({"type": "action"}),
        json.dumps({"type": "action", "action": {"type": "teleport"}}),
        json.dumps({"type": "action", "action": {"type": "move_to", "residentId": "a"}}),
        json.dumps({"type": "action", "action": {"type": "move_to", "target": {"x": 1, "y": 1}}}),
        json.dumps({"type": "action", "action": {"type": "leave_game"}}),
        json.dumps({"type": "action", "action": {"type": "leave_game", "residentId": 1}}),
    ],
)
def test_malformed_actions_get_a_bounded_error(client: TestClient, frame: str) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(frame)
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["code"] == "bad_message"
        assert 0 < len(reply["message"]) <= ERROR_MAX_LENGTH
        assert positions(greet(ws))["a"] == {"x": 0, "y": 50}


def test_outbox_flags_overflow_instead_of_growing() -> None:
    outbox = Outbox(limit=2)
    for text in ("a", "b", "c", "d"):
        outbox.put(text)
    assert list(outbox.replies) == ["a", "b"]
    assert outbox.overflowed.is_set()


def test_outbox_keeps_only_the_latest_state_and_sends_it_after_replies() -> None:
    async def drain(outbox: Outbox) -> list[str]:
        return [await outbox.get() for _ in range(3)]

    outbox = Outbox(limit=2)
    outbox.put_state("state 1")
    outbox.put("welcome")
    outbox.put_state("state 2")
    outbox.put("result")
    assert asyncio.run(drain(outbox)) == ["welcome", "result", "state 2"]
    assert outbox.state is None
    assert not outbox.replies


def test_a_burst_of_moves_does_not_overflow_other_connections() -> None:
    game = Game(SMALL_WORLD)
    watcher = Outbox(limit=2)
    game.watch(watcher)
    for _ in range(1000):
        game.broadcast_world()
    assert not watcher.overflowed.is_set()
    assert not watcher.replies
    assert watcher.state is not None


def test_a_connection_that_falls_behind_is_closed() -> None:
    # The test client reads between frames, so force the overflow: no room for the welcome.
    client = TestClient(create_app(SMALL_WORLD, outbox_limit=0))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(HELLO))
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == CLOSE_POLICY_VIOLATION


def leave(resident_id: str) -> str:
    return json.dumps(
        {"type": "action", "action": {"type": "leave_game", "residentId": resident_id}}
    )


def test_a_resident_who_leaves_is_marked_and_can_no_longer_act(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(leave("a"))
        assert ws.receive_json() == {"type": "action_result", "ok": True}
        residents = ws.receive_json()["world"]["residents"]
        assert [(r["id"], r["left"]) for r in residents] == [("a", True), ("b", False)]
        ws.send_text(move("a", 1, 1))
        assert ws.receive_json()["code"] == "resident_left"
        # The game goes on for everyone else.
        ws.send_text(move("b", 1, 1))
        assert ws.receive_json()["ok"] is True


def test_remaining_time_counts_down_and_actions_end_with_the_game(
    client: TestClient, clock: FakeClock
) -> None:
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        clock.now = 100
        assert greet(ws)["game"] == {"ended": False, "remainingSeconds": GAME_DURATION - 100}
        greet(watcher)
        clock.now = GAME_DURATION
        # The first frame after the deadline ends the game before it is handled.
        ws.send_text(move("a", 1, 1))
        assert ws.receive_json()["code"] == "game_over"
        for connection in (ws, watcher):
            state = connection.receive_json()
            assert state["world"]["game"] == {"ended": True, "remainingSeconds": 0}
            assert positions(state["world"])["a"] == {"x": 0, "y": 50}
    with client.websocket_connect("/ws") as later:
        assert greet(later)["game"]["ended"] is True


def test_the_clock_ends_the_game_without_any_action() -> None:
    # Entering the client runs the app's lifespan, which starts the timer.
    with TestClient(create_app(SMALL_WORLD, duration=0.2)) as client:
        with client.websocket_connect("/ws") as ws:
            world = greet(ws)
            if not world["game"]["ended"]:
                world = ws.receive_json()["world"]
    assert world["game"] == {"ended": True, "remainingSeconds": 0}


def mine(resident_id: str, pool_id: str) -> str:
    action = {"type": "mine", "residentId": resident_id, "poolId": pool_id}
    return json.dumps({"type": "action", "action": action})


def resident_view(world: dict[str, Any], resident_id: str) -> dict[str, Any]:
    return next(r for r in world["residents"] if r["id"] == resident_id)


def test_walking_costs_tokens_and_mining_pays_them_back(
    client: TestClient, clock: FakeClock
) -> None:
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        greet(ws)
        greet(watcher)
        ws.send_text(move("b", 12.5, 10))
        assert ws.receive_json()["ok"] is True
        assert resident_view(ws.receive_json()["world"], "b")["tokens"] == 97
        watcher.receive_json()
        clock.now = 2
        ws.send_text(mine("b", "p1"))
        assert ws.receive_json()["ok"] is True
        world = ws.receive_json()["world"]
        assert world["pools"][0]["reserve"] == 40 - MINE_YIELD
        assert resident_view(world, "b")["mining"] == {
            "poolId": "p1",
            "amount": MINE_YIELD,
            "chip": "MK1",
            "remainingSeconds": MINE_SECONDS,
        }
        watcher.receive_json()
        ws.send_text(move("b", 12, 10))
        assert ws.receive_json()["code"] == "busy"
        clock.now = 2 + MINE_SECONDS
        # The first frame after the job ends pays it out and tells everyone, then is handled.
        ws.send_text(move("b", 12.5, 10))
        assert ws.receive_json()["ok"] is True
        for connection in (ws, watcher):
            b = resident_view(connection.receive_json()["world"], "b")
            assert b["tokens"] == 97 + MINE_YIELD
            assert "mining" not in b


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (mine("a", "p1"), "not_at_pool"),
        (mine("b", "p9"), "unknown_pool"),
    ],
)
def test_token_and_mining_rejections_reach_the_sender(
    client: TestClient, frame: str, code: str
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(frame)
        assert ws.receive_json()["code"] == code


def test_a_walk_beyond_the_tokens_left_is_refused(client: TestClient, clock: FakeClock) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(move("a", 50, 0))
        assert ws.receive_json()["ok"] is True
        assert resident_view(ws.receive_json()["world"], "a")["tokens"] == 29
        ws.send_text(move("a", 0, 50))
        assert ws.receive_json()["code"] == "busy"
        clock.now = 100
        ws.send_text(move("a", 0, 50))
        assert ws.receive_json()["code"] == "not_enough_tokens"


@pytest.mark.parametrize(
    "action",
    [
        {"type": "mine", "residentId": "a"},
        {"type": "mine", "poolId": "p1"},
        {"type": "mine", "residentId": "a", "poolId": 1},
    ],
)
def test_malformed_mine_actions_get_a_bounded_error(
    client: TestClient, action: dict[str, Any]
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps({"type": "action", "action": action}))
        assert ws.receive_json()["code"] == "bad_message"


def test_the_timer_pays_out_mining_without_any_action() -> None:
    job = MiningJob("p1", ends_at=0.2, amount=5, chip="MK1")
    miner = replace(SMALL_WORLD.residents[1], position=Position(10, 10), mining=job)
    world = replace(SMALL_WORLD, residents=(SMALL_WORLD.residents[0], miner))
    with TestClient(create_app(world)) as client, client.websocket_connect("/ws") as ws:
        b = resident_view(greet(ws), "b")
        if "mining" in b:
            b = resident_view(ws.receive_json()["world"], "b")
    assert b["tokens"] == 105
    assert "mining" not in b


def test_default_world_gives_everyone_tokens_and_scatters_pools() -> None:
    with TestClient(create_app()).websocket_connect("/ws") as ws:
        world = greet(ws)
    assert {r["tokens"] for r in world["residents"]} == {STARTING_TOKENS}
    assert len(world["pools"]) == POOL_COUNT


def test_a_frame_after_the_deadline_pays_jobs_due_before_it_and_voids_the_rest(
    clock: FakeClock,
) -> None:
    early = replace(SMALL_WORLD.residents[0], mining=MiningJob("p1", GAME_DURATION - 1, 5, "MK1"))
    late = replace(SMALL_WORLD.residents[1], mining=MiningJob("p1", GAME_DURATION + 5, 7, "MK1"))
    world = replace(SMALL_WORLD, residents=(early, late))
    client = TestClient(create_app(world, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        clock.now = GAME_DURATION + 10
        ws.send_text(move("a", 1, 1))
        assert ws.receive_json()["code"] == "game_over"
        final = ws.receive_json()["world"]
    assert final["game"]["ended"] is True
    assert resident_view(final, "a")["tokens"] == 105
    assert resident_view(final, "b")["tokens"] == 100
    assert final["pools"][0]["reserve"] == 47


def test_the_timer_wakes_for_a_job_started_while_it_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aiverse_server.economy.mining.MINE_SECONDS", 0.2)
    miner = replace(SMALL_WORLD.residents[1], position=Position(10, 10))
    world = replace(SMALL_WORLD, residents=(SMALL_WORLD.residents[0], miner))
    # The timer is already asleep until the end of the game when the job starts. The game is
    # short, so a timer that never wakes for the job fails here rather than hangs.
    app = create_app(world, duration=3)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(mine("b", "p1"))
        assert ws.receive_json()["ok"] is True
        assert "mining" in resident_view(ws.receive_json()["world"], "b")
        # No frame follows: only the timer can send this, and it must come before the end.
        paid = ws.receive_json()["world"]
    assert paid["game"]["ended"] is False
    b = resident_view(paid, "b")
    assert b["tokens"] == 100 + MINE_YIELD
    assert "mining" not in b


def sell(resident_id: str, quantity: object) -> str:
    action = {"type": "sell_tokens", "residentId": resident_id, "quantity": quantity}
    return json.dumps({"type": "action", "action": action})


MARKET_WORLD = replace(SMALL_WORLD, market=Market(Position(12, 7), radius=5))


def test_selling_tokens_pays_coins_and_lowers_the_price_for_everyone(clock: FakeClock) -> None:
    client = TestClient(create_app(MARKET_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        world = greet(ws)
        greet(watcher)
        assert world["market"] == {
            "position": {"x": 12, "y": 7},
            "radius": 5,
            "basePrice": MARKET_BASE_PRICE,
            "depth": MARKET_DEPTH,
            "halfLifeSeconds": MARKET_HALF_LIFE,
            "pressure": 0,
            "chips": [
                {"model": "MK1", "price": 300, "tokensPerJob": 30},
                {"model": "MK2", "price": 1200, "tokensPerJob": 60},
                {"model": "MK3", "price": 3000, "tokensPerJob": 100},
            ],
        }
        clock.now = 3
        ws.send_text(sell("b", 30))
        assert ws.receive_json()["ok"] is True
        for connection in (ws, watcher):
            world = connection.receive_json()["world"]
            b = resident_view(world, "b")
            assert b["coins"] == 524
            assert b["tokens"] == 70
            assert b["rank"] == 1
            assert resident_view(world, "a")["rank"] == 2
            assert world["market"]["pressure"] == 30
        # The pressure in a later state has faded with the time since the sale.
        clock.now = 3 + MARKET_HALF_LIFE
        ws.send_text(sell("a", 1))
        assert ws.receive_json()["code"] == "not_at_market"
        ws.send_text(sell("b", 1))
        assert ws.receive_json()["ok"] is True
        assert ws.receive_json()["world"]["market"]["pressure"] == pytest.approx(16)


@pytest.mark.parametrize(
    ("frame", "code"),
    [(sell("b", 0), "invalid_quantity"), (sell("b", 101), "not_enough_tokens")],
)
def test_market_rejections_reach_the_sender(clock: FakeClock, frame: str, code: str) -> None:
    client = TestClient(create_app(MARKET_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(frame)
        assert ws.receive_json()["code"] == code


@pytest.mark.parametrize(
    "action",
    [
        {"type": "sell_tokens", "residentId": "b"},
        {"type": "sell_tokens", "quantity": 1},
        {"type": "sell_tokens", "residentId": "b", "quantity": 1.5},
        {"type": "sell_tokens", "residentId": "b", "quantity": "1"},
        {"type": "sell_tokens", "residentId": "b", "quantity": True},
    ],
)
def test_malformed_sell_actions_get_a_bounded_error(
    client: TestClient, action: dict[str, Any]
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps({"type": "action", "action": action}))
        assert ws.receive_json()["code"] == "bad_message"


def test_a_quantity_too_big_for_a_float_is_refused_and_the_socket_stays_usable(
    clock: FakeClock,
) -> None:
    client = TestClient(create_app(MARKET_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        action = '{"type": "sell_tokens", "residentId": "b", "quantity": 1' + "0" * 400 + "}"
        ws.send_text('{"type": "action", "action": ' + action + "}")
        assert ws.receive_json()["code"] == "not_enough_tokens"
        ws.send_text(sell("b", 1))
        assert ws.receive_json()["ok"] is True


def test_default_world_has_a_market_in_the_middle() -> None:
    with TestClient(create_app()).websocket_connect("/ws") as ws:
        world = greet(ws)
    assert world["market"]["position"] == {"x": 50, "y": 50}
    assert world["market"]["pressure"] == 0


def buy(resident_id: str, model: object) -> str:
    action = {"type": "buy_chip", "residentId": resident_id, "model": model}
    return json.dumps({"type": "action", "action": action})


# b stands at both the market and the pool, with coins for MK1 but not MK2, and no chip.
SHOP_WORLD = replace(
    MARKET_WORLD,
    residents=(
        MARKET_WORLD.residents[0],
        replace(MARKET_WORLD.residents[1], coins=1_000, inventory=Inventory(((TOKEN, 100),))),
    ),
)


def test_a_chip_bought_shows_on_every_page_and_mines(clock: FakeClock) -> None:
    client = TestClient(create_app(SHOP_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        world = greet(ws)
        greet(watcher)
        assert resident_view(world, "b")["chips"] == {}
        ws.send_text(mine("b", "p1"))
        assert ws.receive_json()["code"] == "no_chip"
        ws.send_text(buy("b", "MK2"))
        assert ws.receive_json()["code"] == "not_enough_coins"
        ws.send_text(buy("b", "MK1"))
        assert ws.receive_json()["ok"] is True
        for connection in (ws, watcher):
            b = resident_view(connection.receive_json()["world"], "b")
            assert (b["coins"], b["chips"]) == (700, {"MK1": 1})
        ws.send_text(mine("b", "p1"))
        assert ws.receive_json()["ok"] is True
        b = resident_view(ws.receive_json()["world"], "b")
        assert b["mining"]["chip"] == "MK1"
        assert b["mining"]["amount"] == 30


def test_a_chip_set_aside_in_an_offer_still_shows_as_owned() -> None:
    world = perform(SMALL_WORLD, OfferToSell("a", "b", "MK1", 1, 1))
    assert resident_view(world_view(world, 1).model_dump(by_alias=True), "a")["chips"] == {"MK1": 1}


@pytest.mark.parametrize(
    ("frame", "code"),
    [(buy("b", "MK9"), "unknown_chip"), (buy("a", "MK1"), "not_at_market")],
)
def test_purchase_rejections_reach_the_sender(clock: FakeClock, frame: str, code: str) -> None:
    client = TestClient(create_app(SHOP_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(frame)
        assert ws.receive_json()["code"] == code


@pytest.mark.parametrize(
    "action",
    [
        {"type": "buy_chip", "residentId": "b"},
        {"type": "buy_chip", "model": "MK1"},
        {"type": "buy_chip", "residentId": "b", "model": 1},
    ],
)
def test_malformed_buy_actions_get_a_bounded_error(
    client: TestClient, action: dict[str, Any]
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps({"type": "action", "action": action}))
        assert ws.receive_json()["code"] == "bad_message"


def say(resident_id: str, text: object, to: object = None) -> str:
    action: dict[str, object] = {"type": "say", "residentId": resident_id, "text": text}
    if to is not None:
        action["to"] = to
    return json.dumps({"type": "action", "action": action})


# c joins a and b, so a whisper between a and b has someone to be kept from.
SPEECH_WORLD = replace(
    SMALL_WORLD,
    residents=(
        *SMALL_WORLD.residents,
        Resident(id="c", name="丙", color="#445566", position=Position(25, 25), inventory=TOKENS),
    ),
)


def view_as(resident_id: str | None) -> str:
    return json.dumps({"type": "view_as", "residentId": resident_id})


def test_whispers_reach_only_pages_viewing_as_a_party(clock: FakeClock) -> None:
    client = TestClient(create_app(SPEECH_WORLD, clock=clock))
    with (
        client.websocket_connect("/ws") as ws,
        client.websocket_connect("/ws") as as_b,
        client.websocket_connect("/ws") as as_c,
    ):
        for connection in (ws, as_b, as_c):
            greet(connection)
        for connection, viewer in ((as_b, "b"), (as_c, "c")):
            connection.send_text(view_as(viewer))
            assert connection.receive_json()["type"] == "state"
        # a stands far from everyone: nobody hears a aloud, yet every page shows the line.
        ws.send_text(say("a", "  有人吗？ "))
        assert ws.receive_json() == {"type": "action_result", "ok": True}
        aloud = {"seq": 1, "time": 0.0, "speakerId": "a", "text": "有人吗？", "listeners": []}
        for connection in (ws, as_b, as_c):
            assert connection.receive_json()["world"]["speech"] == [aloud]
        ws.send_text(say("a", "卖你 token", to="b"))
        assert ws.receive_json()["ok"] is True
        whisper = {
            "seq": 2,
            "time": 0.0,
            "speakerId": "a",
            "text": "卖你 token",
            "to": "b",
            "listeners": ["b"],
        }
        assert as_b.receive_json()["world"]["speech"] == [aloud, whisper]
        assert as_c.receive_json()["world"]["speech"] == [aloud]
        # Viewing as nobody, even the page that sent it does not see the whisper.
        world = ws.receive_json()["world"]
        assert world["speech"] == [aloud]
        # Aloud costs 2 tokens, a whisper 1.
        assert resident_view(world, "a")["tokens"] == 100 - 2 - 1
        ws.send_text(view_as("a"))
        assert ws.receive_json()["world"]["speech"] == [aloud, whisper]
        ws.send_text(view_as(None))
        assert ws.receive_json()["world"]["speech"] == [aloud]


def test_states_show_only_the_viewers_offers_and_contracts() -> None:
    rich = tuple(replace(resident, coins=1000) for resident in SPEECH_WORLD.residents)
    world = replace(SPEECH_WORLD, residents=rich)
    # b offers a tokens, a proposes b a contract and b signs it, then a proposes another.
    world = perform(world, OfferToSell("b", "a", TOKEN, 5, 80))
    world = perform(world, ProposeContract("a", "b", False, TOKEN, 10, 100, 3, 60))
    world = perform(world, RespondToEvent("b", 2, True))
    world = perform(replace(world, time=15), ProposeContract("b", "a", True, "MK1", 1, 200, 1, 30))
    shown = world_view(world, 100, viewer="a").model_dump(by_alias=True, exclude_none=True)
    assert shown["offers"] == [
        {
            "eventId": 1,
            "side": "sell",
            "fromId": "b",
            "toId": "a",
            "item": TOKEN,
            "quantity": 5,
            "price": 80,
        }
    ]
    assert shown["contractProposals"] == [
        {
            "eventId": 3,
            "fromId": "b",
            "toId": "a",
            "supplierId": "b",
            "buyerId": "a",
            "item": "MK1",
            "quantity": 1,
            "price": 200,
            "deliveries": 1,
            "durationSeconds": 30,
        }
    ]
    assert shown["contracts"] == [
        {
            "id": 1,
            "supplierId": "b",
            "buyerId": "a",
            "item": TOKEN,
            "quantity": 10,
            "price": 100,
            "deliveriesLeft": 3,
            "escrow": 300,
            "remainingSeconds": 45.0,
        }
    ]
    # The escrow still counts in a's public total.
    assert resident_view(shown, "a")["coins"] == 1000
    for outsider in ("c", None):
        hidden = world_view(world, 100, viewer=outsider)
        assert (hidden.offers, hidden.contract_proposals, hidden.contracts) == ([], [], [])


def test_viewing_as_a_resident_needs_hello_and_a_resident(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(view_as("a"))
        assert ws.receive_json()["code"] == "bad_message"
        greet(ws)
        ws.send_text(view_as("nobody"))
        assert ws.receive_json()["code"] == "bad_message"
        # Refused, so the page still views as nobody.
        ws.send_text(say("a", "secret", to="b"))
        assert ws.receive_json()["ok"] is True
        assert ws.receive_json()["world"]["speech"] == []


@pytest.mark.parametrize(
    "message",
    [{"type": "view_as"}, {"type": "view_as", "residentId": 1}],
)
def test_malformed_view_as_gets_a_bounded_error(
    client: TestClient, message: dict[str, Any]
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps(message))
        assert ws.receive_json()["code"] == "bad_message"


def test_states_carry_only_the_latest_speech(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(view_as("a"))
        ws.receive_json()
        # Whispers, at a token each, so a's 100 tokens last.
        for i in range(SPEECH_VIEW_LIMIT + 3):
            ws.send_text(say("a", str(i), to="b"))
            assert ws.receive_json()["ok"] is True
            speech = ws.receive_json()["world"]["speech"]
        assert len(speech) == SPEECH_VIEW_LIMIT
        assert speech[0]["seq"] == 4
        assert speech[-1]["text"] == str(SPEECH_VIEW_LIMIT + 2)


def test_the_latest_lines_are_counted_among_those_the_viewer_may_see() -> None:
    world = SPEECH_WORLD
    world = perform(world, Say("a", "aloud"))
    for i in range(SPEECH_VIEW_LIMIT):
        world = perform(world, Say("b", str(i), to="c"))
    # a sees no whisper here, so the line aloud is still among a's latest.
    seen = world_view(world, 1, "a").speech
    assert [line.text for line in seen] == ["aloud"]
    assert len(world_view(world, 1, "c").speech) == SPEECH_VIEW_LIMIT


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (say("a", "   "), "empty_speech"),
        (say("a", "字" * 101), "speech_too_long"),
        (say("a", "hi", to="a"), "unknown_listener"),
        (say("a", "hi", to="nobody"), "unknown_listener"),
        (say("nobody", "hi"), "unknown_resident"),
    ],
)
def test_speech_rejections_reach_the_sender(client: TestClient, frame: str, code: str) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(frame)
        assert ws.receive_json()["code"] == code


@pytest.mark.parametrize(
    "action",
    [
        {"type": "say", "residentId": "a"},
        {"type": "say", "text": "hi"},
        {"type": "say", "residentId": "a", "text": 1},
        {"type": "say", "residentId": "a", "text": "hi", "to": 2},
    ],
)
def test_malformed_say_actions_get_a_bounded_error(
    client: TestClient, action: dict[str, Any]
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps({"type": "action", "action": action}))
        assert ws.receive_json()["code"] == "bad_message"


def test_a_lone_surrogate_is_refused_and_states_still_go_out(client: TestClient) -> None:
    # JSON can carry a lone surrogate as an escape; kept, it would make every later state
    # impossible to encode.
    frame = '{"type": "action", "action": {"type": "say", "residentId": "a", "text": "hi \\ud800"}}'
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(frame)
        assert ws.receive_json()["code"] == "invalid_speech"
        ws.send_text(say("a", "still here"))
        assert ws.receive_json()["ok"] is True
        assert ws.receive_json()["world"]["speech"][-1]["text"] == "still here"
