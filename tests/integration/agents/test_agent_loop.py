import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.agents.observation import Observation
from aiverse_server.agents.runner import AgentRunner, Pace, TurnRecord, record_to_json
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.market import quote
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import (
    CancelContract,
    LeaveGame,
    MoveTo,
    OfferToSell,
    ProposeContract,
    RespondToEvent,
    Say,
    SellTokens,
)
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


def test_the_loop_lets_a_walker_be_until_the_walk_ends() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder(lambda seen, tools: tools.move_to(60, 50))

        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)
        await asyncio.sleep(0.05)

        assert len(brain.seen) == 1
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_when_a_walk_ends() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        brain = Recorder(lambda seen, tools: None if seen.time else tools.move_to(60, 50))
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 4.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)

        assert brain.seen[1].me.position == Position(60, 50)
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_stops_when_the_game_ends() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 600
        game.tick()
        await asyncio.wait_for(task, 1)

        assert task.done() and not task.cancelled()

    asyncio.run(scenario())


def test_each_turn_is_recorded_with_what_was_seen_and_the_calls_made() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder(lambda seen, tools: tools.move_to(60, 50))
        records: list[TurnRecord] = []
        runner = AgentRunner(game, "a", brain, Pace(0, 0, 60), records.append)

        task = asyncio.create_task(runner.run())
        await _until(lambda: len(records) == 1)
        task.cancel()

        assert records[0].calls[0].name == "move_to"
        assert json.loads(record_to_json(records[0]))["observation"]["me"]["id"] == "a"

    asyncio.run(scenario())


def test_the_loop_wakes_for_speech_even_while_busy() -> None:
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

    asyncio.run(scenario())


def test_the_loop_wakes_for_the_ending_announcement_even_while_busy() -> None:
    async def scenario() -> None:
        # The notice comes at 10 s, while the 56 unit walk, 22 s long, is still going.
        game, clock = make_game(duration=70)
        game.act(MoveTo("a", Position(90, 90)))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 10.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)

        assert game.world.residents[0].walking is not None
        assert [notice.text for notice in brain.seen[1].notices] == ["The game ends in 60 seconds."]
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_for_an_offer_when_free() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        game.act(OfferToSell("b", "a", TOKEN, 5, 80))
        await _until(lambda: len(brain.seen) == 2)

        assert len(brain.seen[1].offers_to_me) == 1
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_a_party_when_a_contract_ends_not_by_their_doing() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        game.act(ProposeContract("a", "b", False, TOKEN, 10, 100, 2, 60, deposit=10))
        game.act(RespondToEvent("b", 1, True))
        buyer, supplier = Recorder(), Recorder()
        tasks = [
            asyncio.create_task(AgentRunner(game, "a", buyer, Pace(0, 0, 60)).run()),
            asyncio.create_task(AgentRunner(game, "b", supplier, Pace(0, 0, 60)).run()),
        ]
        await _until(lambda: len(buyer.seen) == len(supplier.seen) == 1)

        game.act(CancelContract("b", 1))
        await _until(lambda: len(buyer.seen) == 2)
        await asyncio.sleep(0.05)

        assert buyer.seen[1].ended_contracts[0].deposit_forfeited == 20
        assert len(supplier.seen) == 1, "the supplier cancelled it, nothing to wake them for"
        for task in tasks:
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


class Broken:
    """A brain that fails every turn, counting how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        self.calls += 1
        raise RuntimeError("model unreachable")


@pytest.mark.parametrize("begins_at", [0.0, 100.0])
def test_an_agent_sells_at_the_price_it_saw_though_another_sold_meanwhile(
    begins_at: float,
) -> None:
    at_market = replace(WORLD.residents[1], position=Position(52, 50))
    world = replace(WORLD, residents=(WORLD.residents[0], at_market))

    async def scenario() -> None:
        game, clock = make_game(world)
        clock.now = begins_at
        market = game.world.market
        assert market is not None
        # What 30 tokens fetch at the round's start; it holds whenever in the game that is.
        seen_price = quote(market, begins_at, 30)
        results = []

        def sell(seen: Observation, tools: ResidentTools) -> None:
            game.act(SellTokens("b", 100))
            results.append(tools.sell_tokens(30))

        brain = Recorder(lambda seen, tools: None if results else sell(seen, tools))
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: bool(results))
        task.cancel()

        assert results[0].ok
        assert next(r for r in game.world.residents if r.id == "a").coins == 5000 + seen_price

    asyncio.run(scenario())


def test_a_failing_brain_is_recorded_and_asked_again() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Broken()
        records: list[TurnRecord] = []

        task = asyncio.create_task(AgentRunner(game, "a", brain, QUICK, records.append).run())
        await _until(lambda: brain.calls >= 2)
        task.cancel()

        assert records[0].error == "RuntimeError: model unreachable"

    asyncio.run(scenario())


def test_a_failing_brain_of_a_free_resident_is_asked_again_before_the_idle_wait() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Broken()
        pace = Pace(interval=0.0, retry=0.0, idle=60.0)

        task = asyncio.create_task(AgentRunner(game, "a", brain, pace).run())
        await _until(lambda: brain.calls >= 2, limit=1.0)
        task.cancel()

        assert brain.calls >= 2

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


def test_a_failing_record_does_not_wake_an_idle_resident_early() -> None:
    def full_disk(turn: TurnRecord) -> None:
        raise OSError("no space left on device")

    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder()
        pace = Pace(interval=0.0, retry=0.0, idle=60.0)

        task = asyncio.create_task(AgentRunner(game, "a", brain, pace, full_disk).run())
        await asyncio.sleep(0.2)
        task.cancel()

        assert len(brain.seen) == 1

    asyncio.run(scenario())


def test_the_loop_stops_when_its_resident_leaves() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        brain = Recorder()

        game.act(LeaveGame("a"))
        await asyncio.wait_for(AgentRunner(game, "a", brain, QUICK).run(), 1)

        assert brain.seen == []

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


def test_a_round_ends_at_the_time_the_brain_finishes_without_acting() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        brain = Recorder(lambda seen, tools: setattr(clock, "now", 25.0))
        records: list[TurnRecord] = []
        runner = AgentRunner(game, "a", brain, Pace(0, 0, 60), records.append)

        task = asyncio.create_task(runner.run())
        await _until(lambda: len(records) == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert records[0].time == 0
        assert records[0].ended_at == 25
        assert game.agent_turns["a"][0].ended_at == 25

    asyncio.run(scenario())
