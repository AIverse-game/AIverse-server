import asyncio

import pytest

from aiverse_server.agents.observation import Observation
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import ActionRejected, MoveTo
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
