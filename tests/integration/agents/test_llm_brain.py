import asyncio
import json
from typing import Any

import httpx
import pytest

from aiverse_server.agents.llm_brain import ChatCompletionsBrain, LLMError
from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.runner import AgentRunner, Pace, TurnRecord
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.server.app import Game

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((TOKEN, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((TOKEN, 100),)), coins=5000),
    ),
    pools=(Pool("p1", Position(80, 50), 4, 300),),
    market=Market(Position(50, 50), 5),
)

QUICK = Pace(interval=0.0, retry=0.0, idle=0.05)
"""Real seconds, short enough for tests."""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    # The turn first: it brings the world up to now.
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def me(game: Game, resident_id: str) -> Resident:
    return next(r for r in game.world.residents if r.id == resident_id)


def _reply(message: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": message}]}


def _http(replies: list[httpx.Response], requests: list[httpx.Request]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return replies.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def test_llm_brain_runs_the_tools_the_model_calls() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "buy_chip", "arguments": '{"model": "MK2"}'},
        }
        replies = [
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": "Bought MK2, off to mine."})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain(
            "https://llm.example/v1/", "secret", "vendor/test", client=_http(replies, requests)
        )
        tools = ResidentTools(game, "a")
        remark = await brain.take_turn(look(game, "a"), tools)
        assert remark == "Bought MK2, off to mine."
        assert me(game, "a").inventory.count("MK2") == 1
        assert brain.requests == 2
        first = json.loads(requests[0].content)
        assert requests[0].url == "https://llm.example/v1/chat/completions"
        assert requests[0].headers["Authorization"] == "Bearer secret"
        assert first["model"] == "vendor/test"
        assert {tool["function"]["name"] for tool in first["tools"]} >= {"move_to", "mine"}
        assert "甲" in first["messages"][0]["content"]
        second = json.loads(requests[1].content)["messages"]
        assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"
        assert json.loads(second[-1]["content"])["ok"] is True
        # The next round hears what happened in this one.
        replies.append(httpx.Response(200, json=_reply({"content": "Waiting."})))
        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))
        assert (
            "buy_chip(model='MK2') -> ok"
            in json.loads(requests[2].content)["messages"][1]["content"]
        )
        assert all("secret" not in json.dumps(json.loads(r.content)) for r in requests)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "No auth credentials found"}}),
        httpx.Response(200, json={"error": {"message": "rate limited"}}),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json=_reply({"content": None, "tool_calls": ["oops"]})),
    ],
)
def test_llm_failures_raise_for_the_runner_to_record(response: httpx.Response) -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http([response], []))
        with pytest.raises(LLMError):
            await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

    asyncio.run(scenario())


def test_llm_brain_stops_asking_at_its_request_limit() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        call = {"id": "c", "function": {"name": "remember", "arguments": "{}"}}
        replies = [httpx.Response(200, json=_reply({"tool_calls": [call]})) for _ in range(3)]
        requests: list[httpx.Request] = []
        client = _http(replies, requests)
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=client, request_limit=3)
        tools = ResidentTools(game, "a")
        await brain.take_turn(look(game, "a"), tools)
        assert len(requests) == 3
        assert [c.result.code for c in tools.calls] == ["invalid_arguments"] * 3
        assert await brain.take_turn(look(game, "a"), ResidentTools(game, "a")) is not None
        assert len(requests) == 3

    asyncio.run(scenario())


def test_the_brain_closes_only_a_client_it_made() -> None:
    async def scenario() -> None:
        given = httpx.AsyncClient()
        await ChatCompletionsBrain("http://x", "k", "m", client=given).aclose()
        assert not given.is_closed
        await given.aclose()
        brain = ChatCompletionsBrain("http://x", "k", "m")
        await brain.aclose()
        assert brain._client.is_closed

    asyncio.run(scenario())


def test_failures_never_carry_the_key_into_logs(caplog: pytest.LogCaptureFixture) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value {request.headers['Authorization']}")

    async def scenario() -> None:
        game, _ = make_game()
        client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        brain = ChatCompletionsBrain("http://llm", "sk-secret", "m", client=client)
        records: list[TurnRecord] = []
        runner = AgentRunner(game, "a", brain, QUICK, records.append)
        task = asyncio.create_task(runner.run())
        while not records:
            await asyncio.sleep(0.01)
        task.cancel()
        assert records[0].error == "LLMError: request failed: LocalProtocolError"

    with caplog.at_level("DEBUG"):
        asyncio.run(scenario())
    assert "agent for a failed" in caplog.text
    assert "sk-secret" not in caplog.text


def test_a_provider_quoting_the_key_back_has_it_masked() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        reply = httpx.Response(401, json={"error": {"message": "bad key sk-secret"}})
        brain = ChatCompletionsBrain("http://llm", "sk-secret", "m", client=_http([reply], []))
        with pytest.raises(LLMError, match=r"bad key \*\*\*"):
            await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

    asyncio.run(scenario())


def test_a_residents_request_count_survives_restarting_their_agent() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        first = ChatCompletionsBrain("http://x", "k", "m", client=_http([], []))
        first.requests = 7
        game.start_agent("a", first, "m")
        second = ChatCompletionsBrain("http://x", "k", "m", client=_http([], []))
        game.start_agent("a", second, "m")
        assert second.requests == 7
        second.requests = 9
        game.stop_agent("a")
        third = ChatCompletionsBrain("http://x", "k", "m", client=_http([], []))
        game.start_agent("a", third, "m")
        assert third.requests == 9
        # Another resident has a count of their own.
        other = ChatCompletionsBrain("http://x", "k", "m", client=_http([], []))
        game.start_agent("b", other, "m")
        assert other.requests == 0
        await game.stop_agents()

    asyncio.run(scenario())
