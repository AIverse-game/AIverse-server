import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from aiverse_server.__main__ import parse_agent
from aiverse_server.agents.llm_brain import (
    ChatCompletionsBrain,
    LLMError,
    LLMSettings,
    SettingsError,
    complete,
)
from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.runner import AgentRunner, Pace, TurnRecord, record_to_json
from aiverse_server.agents.scripted_brain import ScriptedBrain
from aiverse_server.agents.tools import TOOLS, ResidentTools, tool_specs
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.chips import CHIP_MODELS
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import ActionRejected, MoveTo, OfferToSell, Say
from aiverse_server.game.clock import next_due
from aiverse_server.game.layout import STARTING_COINS, create_default_world
from aiverse_server.server.app import Game, create_app

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
    return observe(game.world, resident_id, game.begin_turn(resident_id), game.remaining())


def me(game: Game, resident_id: str) -> Resident:
    return next(r for r in game.world.residents if r.id == resident_id)


# The shared way in.


def test_act_performs_broadcasts_and_wakes_subscribers() -> None:
    game, _ = make_game()
    changed = game.subscribe()
    game.act(MoveTo("a", Position(60, 60)))
    assert me(game, "a").walking is not None
    assert changed.is_set()
    game.unsubscribe(changed)


def test_a_rejected_act_changes_nothing_and_wakes_nobody() -> None:
    game, _ = make_game()
    before = game.world
    changed = game.subscribe()
    with pytest.raises(ActionRejected):
        game.act(MoveTo("a", Position(500, 0)))
    assert game.world == before
    assert not changed.is_set()


# Tools.


def test_tools_act_for_their_own_resident_only() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    result = tools.call("buy_chip", {"model": "MK1"})
    assert result.ok
    assert me(game, "a").inventory.count("MK1") == 1
    assert me(game, "b").inventory.count("MK1") == 0
    assert result.status is not None and result.status["coins"] == 4700
    # No tool takes the resident who acts.
    for spec in TOOLS:
        assert "resident_id" not in spec.args.model_fields


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("leave_game", {}, "unknown_tool"),
        ("move_to", {"x": 1}, "invalid_arguments"),
        ("move_to", {"x": True, "y": 2}, "invalid_arguments"),
        ("move_to", {"x": float("nan"), "y": 2}, "invalid_arguments"),
        ("move_to", '{"x": 1, "y": 2, "resident_id": "b"}', "invalid_arguments"),
        ("move_to", "not json", "invalid_arguments"),
        ("move_to", "[1, 2]", "invalid_arguments"),
        ("sell_tokens", '{"quantity": ' + "9" * 5000 + "}", "invalid_arguments"),
        ("sell_tokens", {"quantity": "5"}, "invalid_arguments"),
        ("move_to", {"x": 500, "y": 2}, "out_of_bounds"),
        ("mine", {"pool_id": "p1"}, "no_chip"),
        ("buy_chip", {"model": "MK9"}, "unknown_chip"),
        ("forget", {"note_id": 3}, "unknown_note"),
    ],
)
def test_bad_calls_come_back_as_failures_and_change_nothing(
    name: str, arguments: dict[str, Any] | str, code: str
) -> None:
    game, _ = make_game()
    before = game.world
    tools = ResidentTools(game, "a")
    result = tools.call(name, arguments)
    assert not result.ok
    assert result.code == code
    assert game.world == before
    assert len(tools.calls) == 1
    assert json.loads(result.to_json())["ok"] is False


def test_tools_take_arguments_as_json_text_as_models_send_them() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    assert tools.call("move_to", '{"x": 60, "y": 50}').ok
    assert tools.calls[0].arguments == {"x": 60, "y": 50}
    assert tools.call("remember", '{"text": "pool p1 east", "term": "long_term"}').ok
    note = me(game, "a").memory.long_term[0]
    assert tools.call("forget", {"note_id": note.id}).ok
    assert me(game, "a").memory.long_term == ()


def test_trading_tools_send_and_answer_offers() -> None:
    game, _ = make_game()
    seller = ResidentTools(game, "a")
    assert seller.call(
        "offer_to_sell", {"buyer_id": "b", "kind": "token", "quantity": 10, "price": 150}
    ).ok
    event_id = game.world.events.pending[0].id
    assert (
        ResidentTools(game, "b").call("respond_to_offer", {"event_id": event_id, "accept": True}).ok
    )
    assert me(game, "a").coins == 5150
    assert me(game, "b").inventory.count(TOKEN) == 110


def test_tool_specs_are_function_schemas() -> None:
    specs = tool_specs()
    assert {spec["function"]["name"] for spec in specs} == {spec.name for spec in TOOLS}
    move = next(spec for spec in specs if spec["function"]["name"] == "move_to")
    assert move["type"] == "function"
    assert set(move["function"]["parameters"]["required"]) == {"x", "y"}


# Observations.


def test_observation_shows_self_market_pools_offers_and_ranking() -> None:
    game, _ = make_game()
    game.act(OfferToSell("b", "a", TOKEN, 5, 80))
    seen = look(game, "a")
    assert seen.me.at_market and seen.me.tokens == 100 and seen.me.coins == 5000
    assert seen.market is not None and seen.market.distance == 0
    assert seen.offers_to_me[0].from_id == "b" and seen.offers_to_me[0].side == "sell"
    assert [standing.id for standing in seen.residents] == ["a", "b"]
    assert [resident.id for resident in seen.nearby_residents] == ["b"]
    # The pool is 30 units away, out of sight, so not yet known.
    assert seen.known_pools == ()
    them = look(game, "b")
    assert them.my_offers[0].to_id == "a"
    assert them.me.tokens == 95 and them.me.tokens_in_offers == 5
    assert [pool.id for pool in them.known_pools] == ["p1"]
    assert them.known_pools[0].walk_cost == 20


def test_observation_json_is_compact_and_rounded() -> None:
    game, clock = make_game()
    clock.now = 1 / 3
    shown = json.loads(to_json(look(game, "a")))
    assert shown["time"] == 0.3
    assert shown["me"]["position"] == {"x": 50, "y": 50}


# The loop.


class Recorder:
    """A brain that does what each round's `act` says and keeps what it saw."""

    def __init__(self, act: Callable[[Observation, ResidentTools], object] | None = None) -> None:
        self.seen: list[Observation] = []
        self.act = act

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        self.seen.append(observation)
        if self.act is not None:
            self.act(observation, tools)
        return None


async def _until(condition: Callable[[], bool], limit: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.005)


def test_the_loop_wakes_when_a_walk_ends_and_stops_when_the_game_does() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        brain = Recorder(lambda seen, tools: None if seen.time else tools.move_to(60, 50))
        records: list[TurnRecord] = []
        runner = AgentRunner(game, "a", brain, Pace(interval=0, retry=0, idle=60), records.append)
        task = asyncio.create_task(runner.run())
        await _until(lambda: len(brain.seen) == 1)
        await asyncio.sleep(0.05)
        assert len(brain.seen) == 1, "busy walking, nothing to wake for"
        clock.now = 4.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)
        assert brain.seen[1].me.position == Position(60, 50)
        clock.now = 600
        game.tick()
        await asyncio.wait_for(task, 1)
        assert records[0].calls[0].name == "move_to"
        assert json.loads(record_to_json(records[0]))["observation"]["me"]["id"] == "a"

    asyncio.run(scenario())


def test_the_loop_wakes_for_speech_even_while_busy_and_for_offers_when_free() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        game.act(MoveTo("a", Position(90, 90)))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)
        game.act(Say("b", "你好", to="a"))
        await _until(lambda: len(brain.seen) == 2)
        assert brain.seen[1].speech[0].text == "你好"
        task.cancel()

        game, _ = make_game()
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)
        game.act(OfferToSell("b", "a", TOKEN, 5, 80))
        await _until(lambda: len(brain.seen) == 2)
        assert len(brain.seen[1].offers_to_me) == 1
        task.cancel()

    asyncio.run(scenario())


def test_a_walk_that_ends_while_the_brain_thinks_still_wakes_it() -> None:
    async def scenario() -> None:
        game, clock = make_game()

        def walk_and_wait(seen: Observation, tools: ResidentTools) -> None:
            if seen.time == 0:
                tools.move_to(55, 50)
                # The walk takes 2 s; the brain is still thinking when it ends.
                clock.now = 3.0
                game.tick()

        brain = Recorder(walk_and_wait)
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 2)
        assert brain.seen[1].me.position == Position(55, 50)
        task.cancel()

    asyncio.run(scenario())


def test_an_idle_resident_is_asked_again_and_rounds_keep_their_interval() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0.1, 0.1, 0.0)).run())
        await asyncio.sleep(0.35)
        task.cancel()
        assert 2 <= len(brain.seen) <= 4

    asyncio.run(scenario())


def test_a_failing_brain_is_recorded_and_asked_again() -> None:
    class Broken:
        calls = 0

        async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
            Broken.calls += 1
            raise RuntimeError("model unreachable")

    async def scenario() -> None:
        game, _ = make_game()
        records: list[TurnRecord] = []
        task = asyncio.create_task(AgentRunner(game, "a", Broken(), QUICK, records.append).run())
        await _until(lambda: Broken.calls >= 2)
        task.cancel()
        assert records[0].error == "RuntimeError: model unreachable"

    asyncio.run(scenario())


def test_a_failing_record_is_logged_and_the_loop_goes_on() -> None:
    def full_disk(turn: TurnRecord) -> None:
        raise OSError("no space left on device")

    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, QUICK, full_disk).run())
        await _until(lambda: len(brain.seen) >= 2)
        assert not task.done()
        task.cancel()

    asyncio.run(scenario())


def test_records_are_standard_json_even_with_nan_arguments() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    tools.call("move_to", {"x": float("nan"), "y": 1})
    turn = TurnRecord("a", 0.0, to_json(look(game, "a")), tuple(tools.calls))
    line = record_to_json(turn)
    assert json.loads(line)["calls"][0]["arguments"]["x"] == "nan"


def test_agents_must_drive_residents_of_the_world() -> None:
    with pytest.raises(ValueError, match="nobody"):
        create_app(WORLD, agents={"nobody": Recorder()})


def test_the_loop_stops_when_its_resident_leaves() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        from aiverse_server.game.actions import LeaveGame

        game.act(LeaveGame("a"))
        await asyncio.wait_for(AgentRunner(game, "a", Recorder(), QUICK).run(), 1)

    asyncio.run(scenario())


# The scripted brain.


def test_the_scripted_bot_earns_coins_on_the_default_map() -> None:
    """Plays a fast-forwarded game: each round every bot acts, then the clock jumps to the
    next walk or job that ends."""

    async def scenario() -> None:
        clock = FakeClock()
        game = Game(create_default_world(seed=7), 600, clock)
        brains = {resident.id: ScriptedBrain(seed=7) for resident in game.world.residents}
        while not game.world.ended:
            for resident_id, brain in brains.items():
                if not me(game, resident_id).left:
                    await brain.take_turn(look(game, resident_id), ResidentTools(game, resident_id))
            due = next_due(game.world)
            clock.now = min(due if due is not None else clock.now + 5, 600)
            game.tick()
        # Every bot bought a chip, mined and sold; whether chips pay off is a matter of
        # tuning, see docs/status.md in the client repository.
        assert all(not resident.left for resident in game.world.residents)
        assert all(
            any(resident.inventory.count(chip.model) for chip in CHIP_MODELS)
            for resident in game.world.residents
        )
        assert all(pool.reserve < 150 for pool in game.world.pools)
        assert game.world.market is not None and game.world.market.pressure > 0
        assert max(resident.coins for resident in game.world.residents) > STARTING_COINS

    asyncio.run(scenario())


# The language model brain.


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


# The command line.


def test_agents_are_named_resident_equals_brain() -> None:
    assert parse_agent("r1=scripted") == ("r1", "scripted", None)
    assert parse_agent("r2=llm") == ("r2", "llm", None)
    assert parse_agent("r2=llm:nvidia/x:free") == ("r2", "llm", "nvidia/x:free")
    for bad in ("r1", "=scripted", "r1=gpt", "r1=scripted:x", "r1=openrouter"):
        with pytest.raises(ValueError):
            parse_agent(bad)


# Model settings.


def test_settings_given_override_those_chosen() -> None:
    chosen = LLMSettings("http://page", "page-key", "page-model")
    assert LLMSettings(api_key="server-key").over(chosen) == LLMSettings(
        "http://page", "server-key", "page-model"
    )
    assert LLMSettings().over(chosen) == chosen
    assert LLMSettings(model="m").given() == ("model",)
    assert "page-key" not in repr(chosen)


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        (LLMSettings(None, "k", "m"), "missing_llm_setting"),
        (LLMSettings("http://x", None, None), "missing_llm_setting"),
        (LLMSettings("ftp://x", "k", "m"), "invalid_llm_setting"),
        (LLMSettings("llm.example/v1", "k", "m"), "invalid_llm_setting"),
        (LLMSettings("http://[bad", "k", "m"), "invalid_llm_setting"),
        (LLMSettings("http://x", "sk-secret\nmore", "m"), "invalid_llm_setting"),
        (LLMSettings("http://x", "sk secret", "m"), "invalid_llm_setting"),
        (LLMSettings("http://x", "sk-密钥", "m"), "invalid_llm_setting"),
    ],
)
def test_incomplete_or_malformed_settings_are_refused(settings: LLMSettings, code: str) -> None:
    with pytest.raises(SettingsError) as raised:
        complete(settings)
    assert raised.value.code == code


def test_complete_settings_come_back_in_order() -> None:
    assert complete(LLMSettings("https://x/v1", "k", "m")) == ("https://x/v1", "k", "m")


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


class ClosingBrain:
    def __init__(self) -> None:
        self.closed = False

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def test_a_stopped_runner_closes_its_brain() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = ClosingBrain()
        task = asyncio.create_task(AgentRunner(game, "a", brain, QUICK).run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert brain.closed

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
