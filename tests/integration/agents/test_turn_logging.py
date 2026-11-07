import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from aiverse_server.agents.llm_brain import ChatCompletionsBrain
from aiverse_server.agents.observation import Observation
from aiverse_server.agents.runner import AgentRunner, Pace
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.agents.turn_logger import TurnLogger
from aiverse_server.core.world import Position
from aiverse_server.game.layout import create_default_world
from aiverse_server.server.app import Game


def game_at_market() -> Game:
    world = create_default_world(7)
    world = replace(
        world,
        residents=(replace(world.residents[0], position=Position(75, 75)), *world.residents[1:]),
    )
    return Game(world, clock=lambda: 0)


async def wait_for_log(path: Path) -> None:
    for _ in range(200):
        if path.read_text():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("round was not written")


def test_the_runner_logs_actions_memory_and_timestamps(tmp_path: Path) -> None:
    class PlanningBrain:
        async def take_turn(self, observation: Observation, tools: ResidentTools) -> str:
            tools.call("remember", {"text": "保留返程算力", "term": "long_term"})
            tools.call("remember", {"text": "观察市场", "term": "short_term"})
            tools.buy_program("v1")
            return "准备采集"

    async def scenario() -> dict[str, Any]:
        game = game_at_market()
        with TurnLogger(tmp_path, game.world.residents) as record:
            task = asyncio.create_task(
                AgentRunner(game, "r1", PlanningBrain(), Pace(0, 0, 60), record).run()
            )
            await wait_for_log(tmp_path / "r1.jsonl")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        row: dict[str, Any] = json.loads((tmp_path / "r1.jsonl").read_text())
        return row

    row = asyncio.run(scenario())

    assert row["round"] == 1 and row["resident_name"] == "阿青"
    assert row["before"]["coins"] == 10000 and row["after"]["coins"] == 9400
    assert row["observation"]["long_term_notes"] == []
    assert row["memory_after"]["long_term"][0]["text"] == "保留返程算力"
    assert row["memory_after"]["short_term"][0]["text"] == "观察市场"
    assert row["outputs"] == ["准备采集"]
    assert row["calls"][-1]["coins_change"] == -600
    assert datetime.fromisoformat(row["started_at"]) <= datetime.fromisoformat(row["finished_at"])
    assert datetime.fromisoformat(row["calls"][0]["timestamp"]).tzinfo is not None


def test_model_text_and_private_memory_are_redacted_before_writing(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        key = "key\\secret'quoted"
        step = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal step
            step += 1
            message: dict[str, Any] = {"content": f"下一步 {key}"}
            if step == 1:
                message = {
                    "content": f"先记住 {key}",
                    "tool_calls": [
                        {
                            "id": "1",
                            "function": {
                                "name": "remember",
                                "arguments": json.dumps({"text": key, "term": "long_term"}),
                            },
                        }
                    ],
                }
            body = {
                "choices": [{"message": message, "finish_reason": f"stop {key}"}],
                "usage": {"completion_tokens": 7},
            }
            return httpx.Response(200, json=body)

        game = game_at_market()
        game.remember("r1", key, "short_term")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            brain = ChatCompletionsBrain(
                "https://example.test/v1", key, "model", strategy=key, client=http
            )
            with TurnLogger(tmp_path, game.world.residents) as record:
                task = asyncio.create_task(
                    AgentRunner(game, "r1", brain, Pace(0, 0, 60), record).run()
                )
                await wait_for_log(tmp_path / "r1.jsonl")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        row: dict[str, Any] = json.loads((tmp_path / "r1.jsonl").read_text())
        return row

    row = asyncio.run(scenario())

    assert row["outputs"] == ["先记住 ***", "下一步 ***"]
    # A provider echoing the key in a finish reason is masked like its other text.
    assert row["completions"] == [{"finish_reason": "stop ***", "completion_tokens": 7}] * 2
    assert row["remark"] == "下一步 ***"
    assert row["strategy"] == "***" and row["model"] == "model"
    assert row["observation"]["short_term_notes"][0]["text"] == "***"
    assert row["memory_after"]["long_term"][0]["text"] == "***"
    assert row["calls"][0]["arguments"]["text"] == "***"
    assert "secret" not in json.dumps(row)


@pytest.mark.parametrize("outcome", ["error", "cancelled"])
def test_incomplete_rounds_preserve_the_actions_already_taken(tmp_path: Path, outcome: str) -> None:
    async def scenario() -> dict[str, Any]:
        reached = asyncio.Event()

        class InterruptedBrain:
            async def take_turn(self, observation: Observation, tools: ResidentTools) -> None:
                tools.call("remember", {"text": "未完成计划", "term": "long_term"})
                reached.set()
                if outcome == "error":
                    raise RuntimeError("provider unreachable")
                await asyncio.Event().wait()

        game = game_at_market()
        with TurnLogger(tmp_path, game.world.residents) as record:
            task = asyncio.create_task(
                AgentRunner(game, "r1", InterruptedBrain(), Pace(0, 0, 60), record).run()
            )
            await asyncio.wait_for(reached.wait(), 2)
            if outcome == "error":
                await wait_for_log(tmp_path / "r1.jsonl")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        row: dict[str, Any] = json.loads((tmp_path / "r1.jsonl").read_text())
        return row

    row = asyncio.run(scenario())

    assert row["calls"][0]["name"] == "remember" and row["calls"][0]["result"]["ok"] is True
    assert row["memory_after"]["long_term"][0]["text"] == "未完成计划"
    assert row["error"] is not None
    assert row["cancelled"] is (outcome == "cancelled")


def test_replacing_an_agent_appends_to_the_same_game_file(tmp_path: Path) -> None:
    class WaitingBrain:
        def __init__(self, remark: str) -> None:
            self.remark = remark

        async def take_turn(self, observation: Observation, tools: ResidentTools) -> str:
            return self.remark

    async def scenario() -> list[dict[str, Any]]:
        game = game_at_market()
        with TurnLogger(tmp_path, game.world.residents) as record:
            # Use Game's actual lifecycle and shared recorder for each replacement.
            game = Game(game.world, clock=lambda: 0, record=record)
            game.start_agent("r1", WaitingBrain("第一模型"))
            await wait_for_log(tmp_path / "r1.jsonl")
            game.stop_agent("r1")
            game.start_agent("r1", WaitingBrain("第二模型"))
            for _ in range(200):
                if len((tmp_path / "r1.jsonl").read_text().splitlines()) == 2:
                    break
                await asyncio.sleep(0.005)
            await game.stop_agents()
        return [json.loads(row) for row in (tmp_path / "r1.jsonl").read_text().splitlines()]

    rows = asyncio.run(scenario())

    assert [row["round"] for row in rows] == [1, 2]
    assert [row["remark"] for row in rows] == ["第一模型", "第二模型"]


@pytest.mark.parametrize(
    ("key", "second_key"),
    [
        ("first\\provider-key", "second-provider-key"),
        ("fake-key", "fake-key-longer-secret"),
        ("fake-key-longer-secret", "fake-key"),
    ],
)
def test_used_credentials_remain_redacted_across_models_and_residents(
    tmp_path: Path, key: str, second_key: str
) -> None:
    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        step = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal step
            step += 1
            message: dict[str, Any] = {"content": key if step <= 2 else second_key}
            if step == 1:
                message["tool_calls"] = [
                    {
                        "id": "1",
                        "function": {
                            "name": "remember",
                            "arguments": json.dumps({"text": key, "term": "long_term"}),
                        },
                    },
                    {
                        "id": "2",
                        "function": {
                            "name": "say",
                            "arguments": json.dumps({"text": key, "to": "r2"}),
                        },
                    },
                ]
            return httpx.Response(200, json={"choices": [{"message": message}]})

        class ScriptedReader:
            async def take_turn(self, observation: Observation, tools: ResidentTools) -> str:
                return key

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            world = game_at_market().world
            with TurnLogger(tmp_path, world.residents) as record:
                game = Game(world, clock=lambda: 0, record=record)
                game.start_agent(
                    "r1", ChatCompletionsBrain("https://example.test/v1", key, "first", client=http)
                )
                await wait_for_log(tmp_path / "r1.jsonl")
                game.start_agent(
                    "r1",
                    ChatCompletionsBrain(
                        "https://example.test/v1", second_key, "second", client=http
                    ),
                )
                for _ in range(200):
                    if len((tmp_path / "r1.jsonl").read_text().splitlines()) >= 2:
                        break
                    await asyncio.sleep(0.005)
                game.start_agent("r2", ScriptedReader())
                await wait_for_log(tmp_path / "r2.jsonl")
                await game.stop_agents()
        a = [json.loads(line) for line in (tmp_path / "r1.jsonl").read_text().splitlines()]
        b: dict[str, Any] = json.loads((tmp_path / "r2.jsonl").read_text())
        return a, b

    a, b = asyncio.run(scenario())

    assert len(a) == 2
    assert a[1]["model"] == "second"
    assert a[1]["remark"] == "***"
    assert a[1]["outputs"] == ["***"]
    assert a[1]["observation"]["long_term_notes"][0]["text"] == "***"
    assert a[1]["memory_after"]["long_term"][0]["text"] == "***"
    assert b["observation"]["speech"][0]["text"] == "***"
    assert b["remark"] == "***"
    assert "provider-key" not in json.dumps([a, b])
    assert "longer-secret" not in json.dumps([a, b])
