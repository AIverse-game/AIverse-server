import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.game.lifecycle import GAME_DURATION
from aiverse_server.residents.walking import WALK_SPEED
from aiverse_server.server.app import CLOSE_POLICY_VIOLATION, create_app
from aiverse_server.server.protocol import (
    ERROR_MAX_LENGTH,
    PROTOCOL_VERSION,
    SPEECH_VIEW_LIMIT,
)

HELLO = {"type": "hello", "protocolVersion": PROTOCOL_VERSION}

SMALL_WORLD = World(
    map=WorldMap(size=50),
    residents=(
        Resident(id="a", name="甲", color="#112233", position=Position(0, 50)),
        Resident(id="b", name="乙", color="#AABBCC", position=Position(12.5, 7)),
    ),
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
                "residents": [
                    {
                        "id": "a",
                        "name": "甲",
                        "color": "#112233",
                        "position": {"x": 0, "y": 50},
                        "coins": 0,
                        "spendableCoins": 0,
                        "left": False,
                        "rank": 1,
                    },
                    {
                        "id": "b",
                        "name": "乙",
                        "color": "#AABBCC",
                        "position": {"x": 12.5, "y": 7},
                        "coins": 0,
                        "spendableCoins": 0,
                        "left": False,
                        "rank": 1,
                    },
                ],
                "game": {"ended": False, "remainingSeconds": GAME_DURATION},
                "exchange": {"books": [], "fills": [], "orders": [], "maxOrders": 8},
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
    assert world["map"]["size"] == 150
    assert [r["id"] for r in world["residents"]] == [f"r{n}" for n in range(1, 9)]
    assert {r["coins"] for r in world["residents"]} == {10_000}
    assert {(r["position"]["x"], r["position"]["y"]) for r in world["residents"]} == {(75, 75)}


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


def resident_view(world: dict[str, Any], resident_id: str) -> dict[str, Any]:
    return next(r for r in world["residents"] if r["id"] == resident_id)


def test_a_walk_costs_nothing_but_time(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)

        ws.send_text(move("b", 12.5, 10))
        reply = ws.receive_json()
        b = resident_view(ws.receive_json()["world"], "b")

    assert reply["ok"] is True
    assert b["coins"] == 0
    assert b["walking"] == {"target": {"x": 12.5, "y": 10}, "remainingSeconds": 3 / WALK_SPEED}


@pytest.mark.parametrize(
    "action",
    [
        {"type": "harvest", "residentId": "a", "springId": "p1"},
        {"type": "sell_compute", "residentId": "a", "quantity": 1},
        {"type": "buy_program", "residentId": "a", "tier": "v1"},
        {"type": "gather_data", "residentId": "a", "mineId": "m1"},
        {"type": "build_collector", "residentId": "a", "springId": "p1"},
    ],
)
def test_the_old_actions_are_gone(client: TestClient, action: dict[str, object]) -> None:
    with client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps({"type": "action", "action": action}))

        reply = ws.receive_json()

    assert (reply["type"], reply["code"]) == ("error", "bad_message")


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
        Resident(id="c", name="丙", color="#445566", position=Position(25, 25)),
    ),
)


def view_as(resident_id: str | None) -> str:
    return json.dumps({"type": "view_as", "residentId": resident_id})


ALOUD = {"seq": 1, "time": 0.0, "speakerId": "a", "text": "有人吗？", "listeners": []}
WHISPER = {
    "seq": 1,
    "time": 0.0,
    "speakerId": "a",
    "text": "卖你算力",
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

        ws.send_text(say("a", "卖你算力", to="b"))
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
        ws.send_text(say("a", "卖你算力", to="b"))
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
        # Whispers, which are free, so a can say more than the page keeps.
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
