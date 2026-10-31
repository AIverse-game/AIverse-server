"""Starting and stopping agents from a page, with the server's overrides."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiverse_server.agents.llm_brain import NO_SETTINGS, LLMSettings
from aiverse_server.agents.observation import Observation
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.server.app import create_app
from aiverse_server.server.protocol import PROTOCOL_VERSION

WORLD = World(
    map=WorldMap(size=50),
    residents=(
        Resident("a", "甲", "#112233", Position(0, 0), Inventory(((TOKEN, 100),))),
        Resident("b", "乙", "#445566", Position(10, 10), Inventory(((TOKEN, 100),))),
        Resident("c", "丙", "#778899", Position(20, 20), Inventory(), left=True),
    ),
    pools=(),
)

HELLO = json.dumps({"type": "hello", "protocolVersion": PROTOCOL_VERSION})


class IdleBrain:
    """Never acts; remembers what it was made with and whether it was closed."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.settings = (base_url, api_key, model)
        self.closed = False

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class Brains:
    """The brain factory handed to the app, keeping every brain it made."""

    def __init__(self) -> None:
        self.made: list[IdleBrain] = []

    def __call__(self, base_url: str, api_key: str, model: str) -> IdleBrain:
        brain = IdleBrain(base_url, api_key, model)
        self.made.append(brain)
        return brain


def app_with(brains: Brains, override: LLMSettings = NO_SETTINGS, world: World = WORLD) -> Any:
    return create_app(world, llm_override=override, llm_brain=brains)


def start(resident_id: str, **llm: str) -> str:
    return json.dumps({"type": "start_agent", "residentId": resident_id, "llm": llm})


def stop(resident_id: str) -> str:
    return json.dumps({"type": "stop_agent", "residentId": resident_id})


PAGE = {"baseUrl": "https://page.example/v1", "apiKey": "page-key", "model": "page-model"}


def greet(ws: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Says hello; returns the welcome and the world."""
    ws.send_text(HELLO)
    welcome = ws.receive_json()
    state = ws.receive_json()
    assert welcome["type"] == "welcome" and state["type"] == "state"
    return welcome, state["world"]


def agents(world: dict[str, Any]) -> dict[str, Any]:
    return {r["id"]: r["agent"] for r in world["residents"] if "agent" in r}


def test_a_page_hands_a_resident_to_a_model_and_everyone_sees_it() -> None:
    brains = Brains()
    with TestClient(app_with(brains)) as client:
        with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as other:
            welcome, before = greet(ws)
            greet(other)

            ws.send_text(start("a", **PAGE))
            result = ws.receive_json()
            mine = ws.receive_json()["world"]
            theirs = other.receive_json()["world"]
            # A new connection sees it from its first state.
            with client.websocket_connect("/ws") as late:
                _, first = greet(late)

    assert welcome["llmOverrides"] == [] and agents(before) == {}
    assert result == {"type": "agent_result", "residentId": "a", "ok": True}
    shown = {"a": {"model": "page-model"}}
    assert agents(mine) == agents(theirs) == agents(first) == shown
    assert brains.made[0].settings == ("https://page.example/v1", "page-key", "page-model")


def test_the_config_overrides_what_the_page_chose_and_is_never_sent() -> None:
    brains = Brains()
    override = LLMSettings("https://server.example/v1", "server-secret", "server-model")
    frames: list[str] = []
    with TestClient(app_with(brains, override)) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(HELLO)
        frames += [ws.receive_text(), ws.receive_text()]
        # Blanks are fine for the settings the server fixes.
        ws.send_text(start("a", baseUrl=" ", apiKey="", model="page-model"))
        frames += [ws.receive_text(), ws.receive_text()]

    assert json.loads(frames[0])["llmOverrides"] == ["baseUrl", "apiKey", "model"]
    assert json.loads(frames[2]) == {"type": "agent_result", "residentId": "a", "ok": True}
    assert agents(json.loads(frames[3])["world"]) == {"a": {"model": "server-model"}}
    assert brains.made[0].settings == ("https://server.example/v1", "server-secret", "server-model")
    assert all("server-secret" not in frame for frame in frames)


def test_a_partial_override_fills_in_only_what_it_gives() -> None:
    brains = Brains()
    with (
        TestClient(app_with(brains, LLMSettings(api_key="server-secret"))) as client,
        client.websocket_connect("/ws") as ws,
    ):
        welcome, _ = greet(ws)

        ws.send_text(start("a", baseUrl="http://localhost:9000/v1", model="m"))
        reply = ws.receive_json()

    assert welcome["llmOverrides"] == ["apiKey"]
    assert reply["ok"] is True
    assert brains.made[0].settings == ("http://localhost:9000/v1", "server-secret", "m")


@pytest.mark.parametrize(
    ("resident_id", "llm", "code"),
    [
        ("a", {"baseUrl": "https://x/v1", "model": "m"}, "missing_llm_setting"),
        ("a", {**PAGE, "baseUrl": "  "}, "missing_llm_setting"),
        ("a", {**PAGE, "baseUrl": "page.example/v1"}, "invalid_llm_setting"),
        ("nobody", PAGE, "unknown_resident"),
        ("c", PAGE, "resident_left"),
    ],
)
def test_bad_requests_are_refused_and_start_nothing(
    resident_id: str, llm: dict[str, str], code: str
) -> None:
    brains = Brains()
    with TestClient(app_with(brains)) as client, client.websocket_connect("/ws") as ws:
        greet(ws)

        ws.send_text(start(resident_id, **llm))
        reply = ws.receive_json()
        ws.send_text(stop("a"))
        next_frame = ws.receive_json()

    assert reply["type"] == "agent_result" and reply["ok"] is False
    assert reply["residentId"] == resident_id
    assert reply["code"] == code
    assert "page-key" not in json.dumps(reply)
    # Nothing changed, so no state follows: the next frame answers the next request.
    assert next_frame == {"type": "agent_result", "residentId": "a", "ok": True}
    assert brains.made == []


def test_no_agent_starts_once_the_game_is_over() -> None:
    brains = Brains()
    now = [0.0]
    app = create_app(WORLD, duration=60, clock=lambda: now[0], llm_brain=brains)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        greet(ws)
        now[0] = 60.0
        ws.send_text(start("a", **PAGE))
        # The game ends as the frame comes in, which is news to everyone.
        assert ws.receive_json() == {
            "type": "agent_result",
            "residentId": "a",
            "ok": False,
            "code": "game_over",
            "message": "the game is over",
        }
        assert ws.receive_json()["world"]["game"]["ended"] is True
    assert brains.made == []


def test_stopping_and_replacing_an_agent_closes_the_old_brain() -> None:
    brains = Brains()
    with TestClient(app_with(brains)) as client, client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(start("a", **PAGE))
        ws.receive_json(), ws.receive_json()

        ws.send_text(start("a", **{**PAGE, "model": "second"}))
        replaced = ws.receive_json(), ws.receive_json()["world"]
        ws.send_text(stop("a"))
        stopped = ws.receive_json(), ws.receive_json()["world"]
        ws.send_text(stop("nobody"))
        unknown = ws.receive_json()

    assert replaced[0]["ok"] is True
    assert agents(replaced[1]) == {"a": {"model": "second"}}
    assert stopped[0] == {"type": "agent_result", "residentId": "a", "ok": True}
    assert agents(stopped[1]) == {}
    assert unknown["code"] == "unknown_resident"
    assert [brain.closed for brain in brains.made] == [True, True]


def test_an_agent_whose_resident_leaves_is_dropped() -> None:
    brains = Brains()
    with TestClient(app_with(brains)) as client, client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(start("a", **PAGE))
        ws.receive_json(), ws.receive_json()
        leave = {"type": "leave_game", "residentId": "a"}
        ws.send_text(json.dumps({"type": "action", "action": leave}))
        assert ws.receive_json() == {"type": "action_result", "ok": True}
        world = ws.receive_json()["world"]
        # The leaving is sent first; the agent notices and stops right after.
        if agents(world):
            world = ws.receive_json()["world"]
        assert agents(world) == {}
    assert brains.made[0].closed


def test_agents_started_on_the_command_line_show_up_and_can_be_stopped() -> None:
    brain = IdleBrain("http://x", "k", "m")
    with (
        TestClient(create_app(WORLD, agents={"b": brain})) as client,
        client.websocket_connect("/ws") as ws,
    ):
        _, first = greet(ws)

        ws.send_text(stop("b"))
        reply = ws.receive_json()
        after = ws.receive_json()["world"]

    assert agents(first) == {"b": {}}
    assert reply["ok"] is True
    assert agents(after) == {}
    assert brain.closed


@pytest.mark.parametrize(
    "frame",
    [
        start("a", **PAGE),
        stop("a"),
    ],
)
def test_agent_requests_need_hello_first(frame: str) -> None:
    with TestClient(app_with(Brains())) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(frame)
        reply = ws.receive_json()
        assert reply["type"] == "error" and reply["code"] == "bad_message"


@pytest.mark.parametrize(
    "llm",
    [
        {"apiKey": ["page-key"]},
        {"apiKey": "page-key" + "x" * 3000},
        {"baseUrl": 7, "apiKey": "page-key"},
    ],
)
def test_malformed_settings_are_refused_without_echoing_them(llm: dict[str, Any]) -> None:
    with TestClient(app_with(Brains())) as client, client.websocket_connect("/ws") as ws:
        greet(ws)
        ws.send_text(json.dumps({"type": "start_agent", "residentId": "a", "llm": llm}))
        reply = ws.receive_json()
        assert reply["type"] == "error" and reply["code"] == "bad_message"
        assert "page-key" not in reply["message"]
