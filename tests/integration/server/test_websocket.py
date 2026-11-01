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
    PricePoint,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.chips import CHIP_MODELS
from aiverse_server.economy.market import (
    MARKET_BASE_PRICE,
    MARKET_DEPTH,
    MARKET_HALF_LIFE,
    MARKET_RESTING_PRESSURE,
)
from aiverse_server.economy.mining import MINE_SECONDS
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.layout import POOL_COUNT, STARTING_TOKENS
from aiverse_server.game.lifecycle import GAME_DURATION
from aiverse_server.residents.walking import WALK_SPEED
from aiverse_server.server.app import CLOSE_POLICY_VIOLATION, create_app
from aiverse_server.server.protocol import (
    ERROR_MAX_LENGTH,
    PROTOCOL_VERSION,
    SPEECH_VIEW_LIMIT,
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
                        "spendableCoins": 0,
                        "rigBalance": 0,
                        "carriedChips": {"MK1": 1},
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
                        "spendableCoins": 0,
                        "rigBalance": 0,
                        "carriedChips": {"MK1": 1},
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
                "rigs": [],
                "rigRules": {
                    "price": 8000,
                    "capacity": 4,
                    "cycleSeconds": 20,
                    "yieldMultiplier": 2,
                    "rentPer100": 50,
                    "energyPer100": 20,
                },
            },
        }


def test_default_app_serves_the_fixed_layout() -> None:
    with TestClient(create_app()).websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(HELLO))
        ws.receive_json()
        world = ws.receive_json()["world"]
    assert world["map"]["size"] == 150
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
        acknowledged = ws.receive_json()
        setting_off = ws.receive_json()
        # Halfway there, the next state shows b on the way.
        clock.now = 10 / WALK_SPEED
        ws.send_text(move("a", 0, 49))
        assert ws.receive_json()["ok"] is True
        halfway = resident_view(ws.receive_json()["world"], "b")
        # The frame first brings b in and tells everyone, then b, no longer busy, may act.
        clock.now = 20 / WALK_SPEED
        ws.send_text(move("b", 32.5, 7))
        assert ws.receive_json()["ok"] is True
        arrived = resident_view(ws.receive_json()["world"], "b")

    assert acknowledged == {"type": "action_result", "ok": True}
    assert setting_off["type"] == "state"
    b = resident_view(setting_off["world"], "b")
    assert b["position"] == {"x": 12.5, "y": 7}
    assert b["walking"] == {"target": {"x": 32.5, "y": 7}, "remainingSeconds": 20 / WALK_SPEED}
    assert halfway["position"] == {"x": 22.5, "y": 7}
    assert arrived["position"] == {"x": 32.5, "y": 7}
    assert "walking" not in arrived


def test_moves_reach_every_greeted_connection(client: TestClient) -> None:
    with client.websocket_connect("/ws") as mover, client.websocket_connect("/ws") as watcher:
        greet(mover)
        greet(watcher)
        with client.websocket_connect("/ws") as stranger:
            mover.send_text(move("a", 5, 6))
            assert mover.receive_json()["ok"] is True
            seen = [mover.receive_json()["world"], watcher.receive_json()["world"]]
            # Not greeted, so no broadcast was queued: the welcome comes first.
            seen.append(greet(stranger))

    heading = {"x": 5, "y": 6}
    assert [resident_view(world, "a")["walking"]["target"] for world in seen] == [heading] * 3


def test_a_later_connection_sees_where_residents_have_got_to(
    client: TestClient, clock: FakeClock
) -> None:
    with client.websocket_connect("/ws") as mover:
        greet(mover)
        mover.send_text(move("a", 5, 6))
        assert mover.receive_json()["ok"] is True

    clock.now = 100
    with client.websocket_connect("/ws") as later:
        world = greet(later)

    assert positions(world)["a"] == {"x": 5, "y": 6}


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


def test_a_resident_who_leaves_is_shown_as_left(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)

        ws.send_text(leave("a"))
        reply = ws.receive_json()
        residents = ws.receive_json()["world"]["residents"]

    assert reply == {"type": "action_result", "ok": True}
    assert [(r["id"], r["left"]) for r in residents] == [("a", True), ("b", False)]


def test_a_resident_who_left_is_refused_while_the_game_goes_on_for_others(
    client: TestClient,
) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(leave("a"))
        ws.receive_json(), ws.receive_json()

        ws.send_text(move("a", 1, 1))
        refused = ws.receive_json()
        ws.send_text(move("b", 1, 1))
        other = ws.receive_json()

    assert refused["code"] == "resident_left"
    assert other["ok"] is True


def test_remaining_time_counts_down(client: TestClient, clock: FakeClock) -> None:
    with client.websocket_connect("/ws") as ws:
        clock.now = 100
        world = greet(ws)

    assert world["game"] == {"ended": False, "remainingSeconds": GAME_DURATION - 100}


def test_the_first_frame_after_the_deadline_ends_the_game_for_everyone(
    client: TestClient, clock: FakeClock
) -> None:
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        greet(ws)
        greet(watcher)

        clock.now = GAME_DURATION
        ws.send_text(move("a", 1, 1))
        reply = ws.receive_json()
        ended = [ws.receive_json()["world"], watcher.receive_json()["world"]]
    with client.websocket_connect("/ws") as later:
        latest = greet(later)

    # The game ends before the frame is handled, so the move is refused.
    assert reply["code"] == "game_over"
    for world in ended:
        assert world["game"] == {"ended": True, "remainingSeconds": 0}
        assert positions(world)["a"] == {"x": 0, "y": 50}
    assert latest["game"]["ended"] is True


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


def test_a_walk_spends_tokens_shown_in_the_next_state(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)

        ws.send_text(move("b", 12.5, 10))
        reply = ws.receive_json()
        b = resident_view(ws.receive_json()["world"], "b")

    assert reply["ok"] is True
    assert b["tokens"] == 97


def test_a_mining_job_is_shown_and_paid_out_to_every_page(
    client: TestClient, clock: FakeClock
) -> None:
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        greet(ws)
        greet(watcher)
        ws.send_text(move("b", 12.5, 10))
        assert ws.receive_json()["ok"] is True
        ws.receive_json(), watcher.receive_json()

        clock.now = 2
        ws.send_text(mine("b", "p1"))
        assert ws.receive_json()["ok"] is True
        started = [ws.receive_json()["world"], watcher.receive_json()["world"]]
        # The first frame after the job ends pays it out and tells everyone, then is handled.
        clock.now = 2 + MINE_SECONDS
        ws.send_text(move("b", 12.5, 10))
        assert ws.receive_json()["ok"] is True
        paid = [resident_view(c.receive_json()["world"], "b") for c in (ws, watcher)]

    for world in started:
        assert world["pools"][0]["reserve"] == 40 - MINE_YIELD
        assert resident_view(world, "b")["mining"] == {
            "poolId": "p1",
            "amount": MINE_YIELD,
            "chip": "MK1",
            "remainingSeconds": MINE_SECONDS,
        }
    for b in paid:
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

        clock.now = 100
        ws.send_text(move("a", 0, 50))
        reply = ws.receive_json()

    assert reply["code"] == "not_enough_tokens"


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


def test_the_timer_brings_a_walker_in_without_any_action() -> None:
    # Half a unit takes 0.2 s. The game is short, so a timer that never wakes for the walk
    # fails here rather than hangs.
    app = create_app(SMALL_WORLD, duration=3)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(move("b", 12.5, 7.5))
        assert ws.receive_json()["ok"] is True
        assert "walking" in resident_view(ws.receive_json()["world"], "b")

        # No frame follows: only the timer can send this, and it must come before the end.
        arrived = ws.receive_json()["world"]

    assert arrived["game"]["ended"] is False
    b = resident_view(arrived, "b")
    assert b["position"] == {"x": 12.5, "y": 7.5}
    assert "walking" not in b


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


def test_states_show_the_market_and_its_prices(clock: FakeClock) -> None:
    with TestClient(create_app(MARKET_WORLD, clock=clock)).websocket_connect("/ws") as ws:
        world = greet(ws)

    assert world["market"] == {
        "position": {"x": 12, "y": 7},
        "radius": 5,
        "basePrice": MARKET_BASE_PRICE,
        "depth": MARKET_DEPTH,
        "halfLifeSeconds": MARKET_HALF_LIFE,
        "restingPressure": MARKET_RESTING_PRESSURE,
        "pressure": 0,
        "chips": [
            {"model": "MK1", "price": 300, "tokensPerJob": 30},
            {"model": "MK2", "price": 1200, "tokensPerJob": 60},
            {"model": "MK3", "price": 3000, "tokensPerJob": 100},
        ],
        "priceHistory": [],
    }


def test_states_show_the_price_noted_through_a_quiet_stretch_and_a_sale(clock: FakeClock) -> None:
    noted = Market(Position(12, 7), radius=5, history=(PricePoint(0.0, MARKET_BASE_PRICE),))
    client = TestClient(create_app(replace(MARKET_WORLD, market=noted), clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)

        clock.now = 12.0
        ws.send_text(sell("b", 30))
        assert ws.receive_json()["ok"] is True
        world = ws.receive_json()["world"]

    history = world["market"]["priceHistory"]
    times = [point["time"] for point in history]
    prices = [point["price"] for point in history]
    # Noted at 0, 5 and 10 s with no sale, then before and after the sale at 12 s.
    assert times == [0.0, 5.0, 10.0, 12.0, 12.0]
    assert prices[0] == MARKET_BASE_PRICE
    assert prices[0] < prices[1] < prices[2] < prices[3]
    assert prices[4] < prices[3]


def test_selling_tokens_pays_coins_and_lowers_the_price_for_everyone(clock: FakeClock) -> None:
    client = TestClient(create_app(MARKET_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        greet(ws)
        greet(watcher)

        ws.send_text(sell("b", 30))
        reply = ws.receive_json()
        seen = [ws.receive_json()["world"], watcher.receive_json()["world"]]

    assert reply["ok"] is True
    for world in seen:
        b = resident_view(world, "b")
        # 30 tokens into an untouched market, as in the market's unit tests.
        assert (b["coins"], b["tokens"], b["rank"]) == (578, 70, 1)
        assert resident_view(world, "a")["rank"] == 2
        assert world["market"]["pressure"] == 30


def test_a_later_state_shows_the_pressure_faded_since_the_sale(clock: FakeClock) -> None:
    client = TestClient(create_app(MARKET_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(sell("b", 30))
        ws.receive_json(), ws.receive_json()

        clock.now = MARKET_HALF_LIFE
        ws.send_text(sell("b", 1))
        assert ws.receive_json()["ok"] is True
        world = ws.receive_json()["world"]

    # Halfway from 30 to the resting -200, plus the token just sold.
    assert world["market"]["pressure"] == pytest.approx(-84)


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (sell("b", 0), "invalid_quantity"),
        (sell("b", 101), "not_enough_tokens"),
        (sell("a", 1), "not_at_market"),
    ],
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
    assert world["market"]["position"] == {"x": 75, "y": 75}
    # The real clock has barely moved the pressure since the world was made: about 2.3 tokens
    # a second at first, so a few seconds of slack.
    assert world["market"]["pressure"] == pytest.approx(0, abs=10)


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


def test_a_chip_bought_shows_on_every_page(clock: FakeClock) -> None:
    client = TestClient(create_app(SHOP_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as watcher:
        before = resident_view(greet(ws), "b")
        greet(watcher)

        ws.send_text(buy("b", "MK1"))
        reply = ws.receive_json()
        after = [resident_view(c.receive_json()["world"], "b") for c in (ws, watcher)]

    assert before["chips"] == {}
    assert reply["ok"] is True
    assert [(b["coins"], b["chips"]) for b in after] == [(700, {"MK1": 1})] * 2


def test_a_chip_bought_mines(clock: FakeClock) -> None:
    client = TestClient(create_app(SHOP_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(buy("b", "MK1"))
        assert ws.receive_json()["ok"] is True
        ws.receive_json()

        ws.send_text(mine("b", "p1"))
        reply = ws.receive_json()
        b = resident_view(ws.receive_json()["world"], "b")

    assert reply["ok"] is True
    assert b["mining"]["chip"] == "MK1"
    assert b["mining"]["amount"] == 30


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (buy("b", "MK9"), "unknown_chip"),
        (buy("a", "MK1"), "not_at_market"),
        (buy("b", "MK2"), "not_enough_coins"),
        (mine("b", "p1"), "no_chip"),
    ],
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


ALOUD = {"seq": 1, "time": 0.0, "speakerId": "a", "text": "有人吗？", "listeners": []}
WHISPER = {
    "seq": 1,
    "time": 0.0,
    "speakerId": "a",
    "text": "卖你 token",
    "to": "b",
    "listeners": ["b"],
}


def test_lines_said_aloud_reach_every_page(clock: FakeClock) -> None:
    client = TestClient(create_app(SPEECH_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as as_c:
        greet(ws)
        greet(as_c)
        as_c.send_text(view_as("c"))
        assert as_c.receive_json()["type"] == "state"

        # a stands far from everyone: nobody hears a aloud, yet every page shows the line,
        # also one viewing as c, who did not hear it.
        ws.send_text(say("a", "  有人吗？ "))
        reply = ws.receive_json()
        speech = [c.receive_json()["world"]["speech"] for c in (ws, as_c)]

    assert reply == {"type": "action_result", "ok": True}
    assert speech == [[ALOUD], [ALOUD]]


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

        ws.send_text(say("a", "卖你 token", to="b"))
        assert ws.receive_json()["ok"] is True
        to_b, to_c, to_sender = (c.receive_json()["world"]["speech"] for c in (as_b, as_c, ws))

    assert to_b == [WHISPER]
    assert to_c == []
    # Viewing as nobody, even the page that sent it does not see the whisper.
    assert to_sender == []


def test_changing_whom_a_page_views_as_resends_the_speech_it_may_see(clock: FakeClock) -> None:
    client = TestClient(create_app(SPEECH_WORLD, clock=clock))
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(say("a", "卖你 token", to="b"))
        assert ws.receive_json()["ok"] is True
        ws.receive_json()

        ws.send_text(view_as("a"))
        as_a = ws.receive_json()["world"]["speech"]
        ws.send_text(view_as(None))
        as_nobody = ws.receive_json()["world"]["speech"]

    assert as_a == [WHISPER]
    assert as_nobody == []


def test_viewing_as_a_resident_needs_hello_first(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text(view_as("a"))
        reply = ws.receive_json()

    assert reply["code"] == "bad_message"


def test_viewing_as_someone_not_in_the_world_is_refused(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)

        ws.send_text(view_as("nobody"))
        reply = ws.receive_json()
        ws.send_text(say("a", "secret", to="b"))
        assert ws.receive_json()["ok"] is True
        speech = ws.receive_json()["world"]["speech"]

    assert reply["code"] == "bad_message"
    # Refused, so the page still views as nobody.
    assert speech == []


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
