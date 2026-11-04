import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from aiverse_server.agents.llm_brain import ChatCompletionsBrain, LLMError
from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.runner import AgentRunner, Pace, TurnRecord
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Position, Resident, Spring, World, WorldMap
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.towers import TOWER_PRICE
from aiverse_server.game.actions import MoveTo
from aiverse_server.server.app import Game

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((ENERGY, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((ENERGY, 100),)), coins=5000),
    ),
    springs=(Spring("p1", Position(80, 50), 4, 300),),
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
            "function": {"name": "buy_crystal", "arguments": '{"tier": "C2"}'},
        }
        replies = [
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": "Bought C2, off to harvest."})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain(
            "https://llm.example/v1/", "secret", "vendor/test", client=_http(replies, requests)
        )

        remark = await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert remark == "Bought C2, off to harvest."
        assert me(game, "a").inventory.count("C2") == 1
        assert brain.requests == 2
        first = json.loads(requests[0].content)
        assert requests[0].url == "https://llm.example/v1/chat/completions"
        assert requests[0].headers["Authorization"] == "Bearer secret"
        assert first["model"] == "vendor/test"
        assert {tool["function"]["name"] for tool in first["tools"]} >= {"move_to", "harvest"}
        assert "甲" in first["messages"][0]["content"]
        second = json.loads(requests[1].content)["messages"]
        assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"
        assert json.loads(second[-1]["content"])["ok"] is True
        assert all("secret" not in json.dumps(json.loads(r.content)) for r in requests)

    asyncio.run(scenario())


def test_the_next_round_hears_what_the_tools_did_in_this_one() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        call = {"id": "c1", "function": {"name": "buy_crystal", "arguments": '{"tier": "C2"}'}}
        replies = [
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": "Bought C2, off to harvest."})),
            httpx.Response(200, json=_reply({"content": "Waiting."})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))
        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        next_round = json.loads(requests[2].content)["messages"][1]["content"]
        assert "buy_crystal(tier='C2') -> ok" in next_round

    asyncio.run(scenario())


def test_the_next_round_hears_what_a_tool_told_the_model() -> None:
    at_spring = replace(WORLD.residents[0], position=Position(80, 50), coins=TOWER_PRICE)
    world = replace(WORLD, residents=(at_spring, WORLD.residents[1]))

    async def scenario() -> None:
        game, _ = make_game(world)
        call = {"id": "c1", "function": {"name": "build_tower", "arguments": '{"spring_id": "p1"}'}}
        replies = [
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": "Built a tower."})),
            httpx.Response(200, json=_reply({"content": "Waiting."})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))
        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        next_round = json.loads(requests[2].content)["messages"][1]["content"]
        assert "build_tower(spring_id='p1') -> ok (built tower 1;" in next_round

    asyncio.run(scenario())


def test_a_round_ends_once_its_tools_leave_the_resident_walking() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        calls = [
            {"id": "c1", "function": {"name": "move_to", "arguments": '{"x": 80, "y": 50}'}},
            {
                "id": "c2",
                "function": {
                    "name": "remember",
                    "arguments": '{"text": "harvest p1", "term": "short_term"}',
                },
            },
        ]
        replies = [
            httpx.Response(200, json=_reply({"content": "Off to p1.", "tool_calls": calls})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))
        tools = ResidentTools(game, "a")

        remark = await brain.take_turn(look(game, "a"), tools)

        assert remark == "Off to p1."
        assert len(requests) == 1
        assert [call.result.ok for call in tools.calls] == [True, True]
        assert me(game, "a").walking is not None

    asyncio.run(scenario())


def test_a_round_ends_once_its_tools_leave_the_resident_harvesting() -> None:
    energy_and_crystal = Inventory(((ENERGY, 100), ("C1", 1)))
    harvester = replace(WORLD.residents[0], position=Position(80, 50), inventory=energy_and_crystal)
    world = replace(WORLD, residents=(harvester, WORLD.residents[1]))

    async def scenario() -> None:
        game, _ = make_game(world)
        call = {"id": "c1", "function": {"name": "harvest", "arguments": '{"spring_id": "p1"}'}}
        replies = [httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]}))]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert len(requests) == 1
        assert me(game, "a").harvesting is not None

    asyncio.run(scenario())


def test_an_empty_reply_after_acting_ends_the_round() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        call = {"id": "c1", "function": {"name": "buy_crystal", "arguments": '{"tier": "C2"}'}}
        replies = [
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": ""})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        remark = await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert remark is None
        assert len(requests) == 2

    asyncio.run(scenario())


def test_a_busy_resident_may_let_a_round_pass_without_a_word() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        game.act(MoveTo("a", Position(80, 50)))
        replies = [httpx.Response(200, json=_reply({"content": ""}))]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        remark = await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert remark is None
        assert len(requests) == 1

    asyncio.run(scenario())


def test_the_brain_keeps_why_each_request_stopped_and_the_api_tokens_it_used() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        cut_off = {
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {
                "prompt_tokens": 3000,
                "prompt_tokens_details": {"cached_tokens": 2500},
                "completion_tokens": 8192,
                "completion_tokens_details": {"reasoning_tokens": 8100},
                "total_tokens": 11192,
            },
        }
        done = {
            "choices": [{"message": {"content": "Waiting."}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 40},
        }
        replies = [httpx.Response(200, json=cut_off), httpx.Response(200, json=done)]
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, []))

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert brain.completions == [
            {
                "finish_reason": "length",
                "prompt_tokens": 3000,
                "cached_tokens": 2500,
                "completion_tokens": 8192,
                "reasoning_tokens": 8100,
                "total_tokens": 11192,
            },
            {"finish_reason": "stop", "completion_tokens": 40},
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "12 tokens",
        {"prompt_tokens": True, "total_tokens": "12", "completion_tokens": 1.5},
        {"prompt_tokens_details": "none", "completion_tokens_details": None},
        {"prompt_tokens_details": {}, "completion_tokens_details": {"reasoning_tokens": False}},
    ],
)
def test_the_brain_ignores_token_counts_the_api_does_not_give_as_numbers(usage: object) -> None:
    async def scenario() -> None:
        game, _ = make_game()
        reply = {
            "choices": [{"message": {"content": "Waiting."}, "finish_reason": "stop"}],
            "usage": usage,
        }
        brain = ChatCompletionsBrain(
            "http://llm", "k", "m", client=_http([httpx.Response(200, json=reply)], [])
        )

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert brain.completions == [{"finish_reason": "stop"}]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("remaining", "configured", "expected"),
    [(180.1, 20000, 20000), (180, 20000, 8192), (60, 20000, 8192), (180, 4096, 4096)],
)
def test_a_request_reduces_its_output_limit_in_the_last_three_minutes(
    remaining: float, configured: int, expected: int
) -> None:
    async def scenario() -> None:
        game, clock = make_game()
        clock.now = 600 - remaining
        requests: list[httpx.Request] = []
        replies = [httpx.Response(200, json=_reply({"content": "Waiting."}))]
        brain = ChatCompletionsBrain(
            "http://llm", "k", "m", client=_http(replies, requests), max_tokens=configured
        )

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        request = json.loads(requests[0].content)
        assert request["max_tokens"] == expected
        prompted = any(
            "Keep reasoning brief" in message["content"] for message in request["messages"]
        )
        assert prompted == (remaining <= 180)

    asyncio.run(scenario())


def test_a_round_reduces_the_limit_when_it_enters_the_last_three_minutes() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        clock.now = 419
        call = {"id": "c1", "function": {"name": "buy_crystal", "arguments": '{"tier": "C2"}'}}
        replies = [
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": "Bought C2."})),
        ]
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                clock.now = 421
            return replies.pop(0)

        brain = ChatCompletionsBrain(
            "http://llm",
            "k",
            "m",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            max_tokens=20000,
        )

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        first, second = (json.loads(request.content) for request in requests)
        assert [first["max_tokens"], second["max_tokens"]] == [20000, 8192]
        assert not any("Keep reasoning brief" in m["content"] for m in first["messages"])
        assert any("Keep reasoning brief" in m["content"] for m in second["messages"])

    asyncio.run(scenario())


def test_the_brain_uses_the_configured_early_warning_threshold() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        game = Game(
            WORLD, duration=600, clock=clock, early_notice_seconds=300, final_notice_seconds=120
        )
        clock.now = 300
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain(
            "http://llm",
            "k",
            "m",
            client=_http([httpx.Response(200, json=_reply({"content": "Waiting."}))], requests),
            max_tokens=20000,
        )

        await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        request = json.loads(requests[0].content)
        prompt = request["messages"][0]["content"]
        assert request["max_tokens"] == 8192
        assert "within 5 minutes" in prompt
        assert "120 seconds before" in prompt

    asyncio.run(scenario())


def test_an_empty_reply_is_asked_again() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        call = {"id": "c1", "function": {"name": "buy_crystal", "arguments": '{"tier": "C2"}'}}
        replies = [
            httpx.Response(200, json=_reply({"content": ""})),
            httpx.Response(200, json=_reply({"content": None, "tool_calls": [call]})),
            httpx.Response(200, json=_reply({"content": "Bought C2."})),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        remark = await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert remark == "Bought C2."
        assert len(requests) == 3
        assert me(game, "a").inventory.count("C2") == 1

    asyncio.run(scenario())


def test_a_round_that_gets_nothing_to_act_on_twice_raises_why() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        cut_off = {"message": {"content": "I'm at the spring and"}, "finish_reason": "length"}
        replies = [
            httpx.Response(200, json=_reply({"content": None})),
            httpx.Response(200, json={"choices": [cut_off]}),
        ]
        requests: list[httpx.Request] = []
        brain = ChatCompletionsBrain("http://llm", "k", "m", client=_http(replies, requests))

        with pytest.raises(LLMError, match="finish_reason: length"):
            await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert len(requests) == 2

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
        after_the_limit = await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

        assert [c.result.code for c in tools.calls] == ["invalid_arguments"] * 3
        assert after_the_limit is not None
        assert len(requests) == 3

    asyncio.run(scenario())


def test_the_brain_leaves_a_client_it_was_given_open() -> None:
    async def scenario() -> None:
        given = httpx.AsyncClient()

        await ChatCompletionsBrain("http://x", "k", "m", client=given).aclose()

        assert not given.is_closed
        await given.aclose()

    asyncio.run(scenario())


def test_the_brain_closes_a_client_it_made() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = ChatCompletionsBrain("http://x", "k", "m")

        await brain.aclose()

        with pytest.raises(RuntimeError, match="client has been closed"):
            await brain.take_turn(look(game, "a"), ResidentTools(game, "a"))

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


def idle_brain(requests: int = 0) -> ChatCompletionsBrain:
    """A brain that has already sent `requests` requests and is given no replies."""
    brain = ChatCompletionsBrain("http://x", "k", "m", client=_http([], []))
    brain.requests = requests
    return brain


def test_a_residents_request_count_survives_replacing_their_agent() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        game.start_agent("a", idle_brain(7), "m")
        second = idle_brain()

        game.start_agent("a", second, "m")

        assert second.requests == 7
        await game.stop_agents()

    asyncio.run(scenario())


def test_a_residents_request_count_survives_stopping_their_agent() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        running = idle_brain()
        game.start_agent("a", running, "m")
        later = idle_brain()

        # The agent sends requests while it runs; stopping it reads the count then.
        running.requests = 9
        game.stop_agent("a")
        game.start_agent("a", later, "m")

        assert later.requests == 9
        await game.stop_agents()

    asyncio.run(scenario())


def test_each_resident_has_a_request_count_of_their_own() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        game.start_agent("a", idle_brain(7), "m")
        other = idle_brain()

        game.start_agent("b", other, "m")

        assert other.requests == 0
        await game.stop_agents()

    asyncio.run(scenario())


def test_page_turn_redacts_credentials_echoed_in_model_output() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []
        replies = [httpx.Response(200, json=_reply({"content": "provider echoed top-secret"}))]
        http = _http(replies, requests)
        brain = ChatCompletionsBrain(
            "https://llm.example/v1", "top-secret", "m", "稳健经营", client=http
        )
        game, _ = make_game()

        game.start_agent("a", brain, "m")
        # Wait for a completed turn, without another paid/model turn.
        for _ in range(100):
            if game.agent_turns.get("a"):
                break
            await asyncio.sleep(0.001)
        await game.stop_agents()
        await http.aclose()

        shown = game.agent_turns["a"][0]
        assert shown.summary == "provider echoed ***"
        assert "top-secret" not in shown.model_dump_json()
        assert "稳健经营" in json.loads(requests[0].content)["messages"][0]["content"]

    asyncio.run(scenario())
