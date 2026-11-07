import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.agents import observation as observation_module
from aiverse_server.agents.observation import Observation
from aiverse_server.agents.runner import AgentRunner, Pace, TurnRecord, record_to_json
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy import exchange
from aiverse_server.economy.ads import CONTENT_ITEM
from aiverse_server.game.actions import (
    BuildAd,
    CancelContract,
    LeaveGame,
    MakeContent,
    MoveTo,
    OfferToSell,
    PlaceContent,
    PlaceOrder,
    ProposeContract,
    RespondToEvent,
    Say,
)
from aiverse_server.server.app import Game

POSTER = "poster"
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((POSTER, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((POSTER, 100),)), coins=5000),
    ),
)

QUICK = Pace(interval=0.0, retry=0.0, idle=0.05)
"""Real seconds, short enough for tests."""


@pytest.fixture(autouse=True)
def posters_have_a_book(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exchange, "EXCHANGE_ITEMS", (POSTER,))
    monkeypatch.setattr(observation_module, "EXCHANGE_ITEMS", (POSTER,))


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


def test_the_loop_wakes_when_someone_fills_a_resting_order() -> None:
    buyer = replace(WORLD.residents[1], position=Position(50, 50))
    world = replace(WORLD, residents=(WORLD.residents[0], buyer))

    async def scenario() -> None:
        game, _ = make_game(world)
        brain = Recorder(
            lambda seen, tools: None if seen.time else tools.place_order("sell", POSTER, 10, 20)
        )
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        game.act(PlaceOrder("b", "buy", POSTER, 10, 20))
        await _until(lambda: len(brain.seen) == 2)

        assert brain.seen[1].exchange.my_fills[0].quantity == 10
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_does_not_wake_a_resident_for_a_fill_they_took() -> None:
    seller = replace(WORLD.residents[1], position=Position(50, 50))
    world = replace(WORLD, residents=(WORLD.residents[0], seller))

    async def scenario() -> None:
        game, _ = make_game(world)
        game.act(PlaceOrder("b", "sell", POSTER, 10, 20))
        brain = Recorder(lambda seen, tools: tools.place_order("buy", POSTER, 10, 20))
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)
        await asyncio.sleep(0.05)

        assert len(brain.seen) == 1
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

        game.act(OfferToSell("b", "a", POSTER, 5, 80))
        await _until(lambda: len(brain.seen) == 2)

        assert len(brain.seen[1].offers_to_me) == 1
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_when_a_compute_job_finishes_even_while_walking() -> None:
    async def scenario() -> None:
        # The ad slot is ready at 12 s, while the 56 unit walk, 22 s long, is still going.
        game, clock = make_game(replace(WORLD, parcels=(Parcel(5, 5, "a"),)))
        game.act(BuildAd("a", "L5-5", "on_demand"))
        game.act(MoveTo("a", Position(90, 90)))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 12.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)

        assert game.world.residents[0].walking is not None
        assert brain.seen[1].land.mine[0].facility == "ad"
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_when_a_content_job_finishes_and_not_when_the_content_is_placed() -> None:
    async def scenario() -> None:
        world = replace(WORLD, parcels=(Parcel(5, 5, "a", Facility("ad", 0.0, (), 30.0)),))
        game, clock = make_game(world)
        game.act(MakeContent("a", "on_demand"))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 6.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)
        game.act(PlaceContent("a", "L5-5"))
        await asyncio.sleep(0.05)

        assert brain.seen[1].me.items[CONTENT_ITEM] == 1
        assert len(brain.seen) == 2
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_for_finished_content_even_after_other_content_went_stale() -> None:
    async def scenario() -> None:
        # One piece shows until 3 s; the job finishes at 6 s, so the count of content falls and
        # rises again by the same amount.
        slot = Facility("ad", 0.0, (3.0,), 30.0)
        game, clock = make_game(replace(WORLD, parcels=(Parcel(5, 5, "a", slot),)))
        game.act(MakeContent("a", "on_demand"))
        game.act(MoveTo("a", Position(90, 90)))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 6.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)

        assert game.world.residents[0].walking is not None
        assert brain.seen[1].me.items[CONTENT_ITEM] == 1
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_an_idle_resident_when_a_compute_job_finishes() -> None:
    async def scenario() -> None:
        game, clock = make_game(replace(WORLD, parcels=(Parcel(5, 5, "a"),)))
        game.act(BuildAd("a", "L5-5", "on_demand"))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 12.0
        game.tick()
        await _until(lambda: len(brain.seen) == 2)

        assert brain.seen[0].compute.my_jobs[0].ends_in == 12.0
        assert brain.seen[1].compute.my_jobs == ()
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_does_not_wake_for_a_job_that_is_still_running() -> None:
    async def scenario() -> None:
        game, clock = make_game(replace(WORLD, parcels=(Parcel(5, 5, "a"),)))
        game.act(BuildAd("a", "L5-5", "self"))
        brain = Recorder()
        task = asyncio.create_task(AgentRunner(game, "a", brain, Pace(0, 0, 60)).run())
        await _until(lambda: len(brain.seen) == 1)

        clock.now = 12.0
        game.tick()
        await asyncio.sleep(0.05)

        assert len(brain.seen) == 1
        task.cancel()

    asyncio.run(scenario())


def test_the_loop_wakes_a_party_when_a_contract_ends_not_by_their_doing() -> None:
    async def scenario() -> None:
        game, _ = make_game()
        game.act(ProposeContract("a", "b", False, POSTER, 10, 100, 2, 60, deposit=10))
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


def test_each_round_is_shown_how_long_the_earlier_rounds_took() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        durations = iter((20.0, 40.0))

        def think(seen: Observation, tools: ResidentTools) -> None:
            clock.now += next(durations, 0.0)

        brain = Recorder(think)

        task = asyncio.create_task(AgentRunner(game, "a", brain, QUICK).run())
        await _until(lambda: len(brain.seen) >= 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        shown = [(seen.last_thinking_seconds, seen.avg_thinking_seconds) for seen in brain.seen]
        assert shown[:3] == [(None, None), (20.0, 20.0), (40.0, 30.0)]

    asyncio.run(scenario())


def test_a_failed_round_counts_toward_the_thinking_time_shown() -> None:
    async def scenario() -> None:
        game, clock = make_game()
        brain = Recorder()

        def fail_after_thinking(observation: Observation, tools: ResidentTools) -> None:
            clock.now += 50.0
            raise RuntimeError("model unreachable")

        brain.act = fail_after_thinking
        task = asyncio.create_task(AgentRunner(game, "a", brain, QUICK).run())
        await _until(lambda: len(brain.seen) >= 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert brain.seen[1].last_thinking_seconds == 50.0

    asyncio.run(scenario())
