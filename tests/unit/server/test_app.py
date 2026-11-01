import asyncio

import pytest

from aiverse_server.agents.observation import Observation
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import ActionRejected, MoveTo
from aiverse_server.game.lifecycle import ENDING_NOTICE_SECONDS
from aiverse_server.server.app import Game, Outbox, create_app

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((TOKEN, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((TOKEN, 100),)), coins=5000),
    ),
    pools=(Pool("p1", Position(80, 50), 4, 300),),
    market=Market(Position(50, 50), 5),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def me(game: Game, resident_id: str) -> Resident:
    return next(r for r in game.world.residents if r.id == resident_id)


class IdleBrain:
    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        return None


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


def test_agents_must_drive_residents_of_the_world() -> None:
    with pytest.raises(ValueError, match="nobody"):
        create_app(WORLD, agents={"nobody": IdleBrain()})


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
    game = Game(WORLD)
    watcher = Outbox(limit=2)
    game.watch(watcher)
    for _ in range(1000):
        game.broadcast_world()
    assert not watcher.overflowed.is_set()
    assert not watcher.replies
    assert watcher.state is not None


def test_the_server_tells_every_resident_a_minute_before_the_game_ends() -> None:
    game, clock = make_game(duration=600)
    clock.now = 539.0
    game.tick()
    unannounced = game.world.notices.entries

    clock.now = 540.0
    game.tick()
    first = game.begin_turn("a")
    second = game.begin_turn("b")
    again = game.begin_turn("a")

    assert unannounced == ()
    assert [notice.text for notice in first.notices] == ["The game ends in 60 seconds."]
    assert first.notices[0].time == 540.0
    assert second.notices == first.notices
    assert again.notices == ()


def test_the_ending_is_announced_once_however_often_the_clock_ticks() -> None:
    game, clock = make_game(duration=600)

    for now in (540.0, 560.0, 580.0):
        clock.now = now
        game.tick()

    assert len(game.world.notices.entries) == 1


def test_the_announcement_wakes_a_waiting_agent() -> None:
    game, clock = make_game(duration=600)
    changed = game.subscribe()
    changed.clear()

    clock.now = 540.0
    game.tick()

    assert changed.is_set()


def test_a_game_no_longer_than_the_warning_announces_nothing() -> None:
    game, clock = make_game(duration=60)

    clock.now = 30.0
    game.tick()

    assert game.world.notices.entries == ()


def test_the_clock_announces_the_ending_on_its_own_when_nothing_else_happens() -> None:
    async def scenario() -> Game:
        game = Game(WORLD, duration=ENDING_NOTICE_SECONDS + 0.2)
        clock = asyncio.create_task(game.run_clock())
        await asyncio.sleep(0.6)
        clock.cancel()
        return game

    game = asyncio.run(scenario())

    assert len(game.world.notices.entries) == 1
