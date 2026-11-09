import asyncio
import json
from dataclasses import replace

import pytest

from aiverse_server.agents.observation import Observation
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.game.actions import ActionRejected, MoveTo, OfferToBuy, RespondToEvent, Say
from aiverse_server.game.clock import next_due
from aiverse_server.game.layout import create_default_world
from aiverse_server.game.lifecycle import EARLY_ENDING_NOTICE_SECONDS, ENDING_NOTICE_SECONDS
from aiverse_server.server.app import Game, Outbox, create_app
from aiverse_server.server.protocol import AgentMood

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), coins=5000),
    ),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def the_crowd(game: Game) -> Crowd:
    return game.world.crowd


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


def test_outbox_keeps_only_the_latest_visitors_and_sends_them_after_the_state() -> None:
    async def drain(outbox: Outbox) -> list[str]:
        return [await outbox.get() for _ in range(3)]

    outbox = Outbox(limit=2)
    outbox.put_visitors("visitors 1")
    outbox.put_state("state")
    outbox.put_visitors("visitors 2")
    outbox.put("result")
    assert asyncio.run(drain(outbox)) == ["result", "state", "visitors 2"]
    assert outbox.visitors is None


def test_pushing_visitors_reaches_every_watcher_with_the_visitors_of_now() -> None:
    walker = Visitor(
        id=1,
        phase="walking",
        until=100.0,
        at=Position(0, 0),
        target=Position(50, 0),
        departs_at=0.0,
    )
    game, clock = make_game(replace(WORLD, crowd=Crowd(visitors=(walker,))))
    first, second = Outbox(), Outbox()
    game.watch(first)
    game.watch(second)

    clock.now = 4.0
    game.push_visitors()

    assert first.visitors is not None
    assert first.visitors == second.visitors
    assert json.loads(first.visitors)["visitors"][0]["position"] == {"x": 10.0, "y": 0.0}


def test_pushing_visitors_in_a_world_without_any_sends_nothing() -> None:
    game, _ = make_game()
    watcher = Outbox()
    game.watch(watcher)

    game.push_visitors()

    assert watcher.visitors is None


def test_a_burst_of_moves_does_not_overflow_other_connections() -> None:
    game = Game(WORLD)
    watcher = Outbox(limit=2)
    game.watch(watcher)
    for _ in range(1000):
        game.broadcast_world()
    assert not watcher.overflowed.is_set()
    assert not watcher.replies
    assert watcher.state is not None


def test_the_server_tells_every_resident_three_minutes_and_one_minute_before_end() -> None:
    game, clock = make_game(duration=600)
    clock.now = 419.0
    game.tick()
    unannounced = game.world.notices.entries

    clock.now = 420.0
    game.tick()
    early_a = game.begin_turn("a")
    early_b = game.begin_turn("b")
    early_again = game.begin_turn("a")
    clock.now = 539.0
    game.tick()
    clock.now = 540.0
    game.tick()
    final_a = game.begin_turn("a")
    final_b = game.begin_turn("b")
    final_again = game.begin_turn("a")

    assert unannounced == ()
    assert [notice.text for notice in early_a.notices] == ["The game ends within 3 minutes."]
    assert early_a.notices[0].time == 420.0
    assert early_b.notices == early_a.notices
    assert early_again.notices == ()
    assert [notice.text for notice in final_a.notices] == ["The game ends in 60 seconds."]
    assert final_a.notices[0].time == 540.0
    assert final_b.notices == final_a.notices
    assert final_again.notices == ()


def test_custom_notice_times_change_when_warnings_are_sent() -> None:
    clock = FakeClock()
    game = Game(
        WORLD, duration=600, clock=clock, early_notice_seconds=300, final_notice_seconds=120
    )
    clock.now = 299
    game.tick()
    before = game.world.notices.entries
    clock.now = 300
    game.tick()
    early = game.begin_turn("a")
    clock.now = 480
    game.tick()
    final = game.begin_turn("a")

    assert before == ()
    assert [notice.text for notice in early.notices] == ["The game ends within 5 minutes."]
    assert [notice.text for notice in final.notices] == ["The game ends in 120 seconds."]


def test_the_ending_is_announced_once_however_often_the_clock_ticks() -> None:
    game, clock = make_game(duration=600)

    for now in (420.0, 440.0, 540.0, 560.0, 580.0):
        clock.now = now
        game.tick()

    assert [notice.text for notice in game.world.notices.entries] == [
        "The game ends within 3 minutes.",
        "The game ends in 60 seconds.",
    ]


def test_the_announcement_wakes_a_waiting_agent() -> None:
    game, clock = make_game(duration=600)
    changed = game.subscribe()
    changed.clear()

    clock.now = 420.0
    game.tick()

    assert changed.is_set()


def test_a_delayed_tick_only_sends_the_latest_ending_notice() -> None:
    game, clock = make_game(duration=600)

    clock.now = 540.0
    game.tick()

    assert [notice.text for notice in game.world.notices.entries] == [
        "The game ends in 60 seconds."
    ]


def test_a_game_no_longer_than_the_warning_announces_nothing() -> None:
    game, clock = make_game(duration=60)

    clock.now = 30.0
    game.tick()

    assert game.world.notices.entries == ()


def test_a_short_game_only_sends_warnings_that_fit_its_duration() -> None:
    game, clock = make_game(duration=EARLY_ENDING_NOTICE_SECONDS)

    clock.now = 120.0
    game.tick()

    assert [notice.text for notice in game.world.notices.entries] == [
        "The game ends in 60 seconds."
    ]


def test_the_clock_announces_the_ending_on_its_own_when_nothing_else_happens() -> None:
    async def scenario() -> Game:
        game = Game(WORLD, duration=ENDING_NOTICE_SECONDS + 0.2)
        clock = asyncio.create_task(game.run_clock())
        await asyncio.sleep(0.6)
        clock.cancel()
        return game

    game = asyncio.run(scenario())

    assert len(game.world.notices.entries) == 1


def test_the_end_of_the_game_is_handed_over_once_with_the_final_world() -> None:
    ends: list[World] = []
    clock = FakeClock()
    game = Game(WORLD, 600, clock, on_end=ends.append)

    for now in (599.0, 600.0, 601.0):
        clock.now = now
        game.tick()

    assert ends == [game.world]
    assert ends[0].ended


def test_a_failing_end_handler_is_logged_and_the_game_still_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(world: World) -> None:
        raise OSError("disk full")

    clock = FakeClock()
    game = Game(WORLD, 600, clock, on_end=fail)

    clock.now = 600.0
    game.tick()

    assert game.world.ended
    assert "handling the end of the game failed" in caplog.text


def test_a_step_of_the_visitors_alone_is_not_broadcast_but_a_walk_arriving_is() -> None:
    entrance = Position(50, 50)
    coming = Visitor(0, "away", 5.0, entrance, entrance)
    walking = replace(WORLD, crowd=Crowd(visitors=(coming,)))
    game, clock = make_game(walking)
    watcher = Outbox()
    game.watch(watcher)

    clock.now = 10.0
    game.tick()
    stepped = the_crowd(game)
    quiet = watcher.state is None
    game.act(MoveTo("a", Position(60, 50)))
    clock.now = 30.0
    game.tick()

    assert stepped.visitors[0].phase == "walking"
    assert quiet
    assert watcher.state is not None
    assert me(game, "a").walking is None


def coins_paid_by_sponsors(late: float) -> int:
    """Plays 300 game seconds of the default world with three busy ad slots, the clock always
    waking `late` seconds after the moment the game asked for."""
    slot = Facility("ad", 0.0, (10_000.0,) * 3)
    world = replace(
        create_default_world(),
        parcels=(Parcel(7, 7, "r1", slot), Parcel(7, 8, "r2", slot), Parcel(6, 6, "r3", slot)),
    )
    game, clock = make_game(world, duration=1_000)
    while clock.now < 300.0:
        due = next_due(game.world)
        clock.now = min((due if due is not None else clock.now + 30.0) + late, 300.0)
        game.tick()
    return sum(t.amount for t in game.world.ledger.transfers if t.channel == "sponsors")


def test_the_sponsors_are_paid_at_every_payday_however_late_the_clock_wakes() -> None:
    on_time = coins_paid_by_sponsors(0.0)

    slightly_late = coins_paid_by_sponsors(0.002)
    late = coins_paid_by_sponsors(0.05)

    assert on_time > 0
    assert slightly_late > on_time * 0.9
    assert late > on_time * 0.9


def test_a_visitor_paying_at_a_door_is_not_news_but_the_payout_at_the_payday_is() -> None:
    entrance = Position(50, 50)
    working = Visitor(0, "staying", 5.0, entrance, entrance, route=("workspace",))
    desk = Parcel(5, 5, "a", Facility("workspace", 0.0))
    game, clock = make_game(replace(WORLD, parcels=(desk,), crowd=Crowd(visitors=(working,))))
    watcher = Outbox()
    game.watch(watcher)

    clock.now = 20.0
    game.tick()
    let_in = game.world.admissions.admitted
    quiet = watcher.state is None
    clock.now = 31.0
    game.tick()

    assert let_in != ()
    assert quiet
    assert watcher.state is not None
    assert me(game, "a").coins == 5_000 + 4


def test_an_agent_shows_thinking_while_its_brain_works_and_cues_what_it_did() -> None:
    class SlowBrain:
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
            await self.release.wait()
            return None

    async def scenario() -> tuple[bool, bool, tuple[str | None, int]]:
        game, _ = make_game()
        brain = SlowBrain()
        game.start_agent("a", brain)
        await asyncio.sleep(0)
        during = game.agent_moods()["a"].thinking
        game.act(Say("a", "hello"))
        mood = game.agent_moods()["a"]
        brain.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await game.stop_agents()
        return during, mood.thinking, (mood.cue, mood.cue_seq)

    during, _, cue = asyncio.run(scenario())

    assert during is True
    assert cue == ("talk", 1)


def test_accepting_an_offer_cues_both_agents_happy_and_stopping_clears_thinking() -> None:
    class Waiting:
        async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
            await asyncio.sleep(1e6)
            return None

    seller = replace(WORLD.residents[1], inventory=Inventory((("bread", 5),)))
    world = replace(WORLD, residents=(WORLD.residents[0], seller))

    async def scenario() -> tuple[dict[str, AgentMood], dict[str, AgentMood]]:
        game, _ = make_game(world)
        game.start_agent("a", Waiting())
        game.start_agent("b", Waiting())
        await asyncio.sleep(0)
        game.act(OfferToBuy("a", "b", "bread", 1, 5))
        game.act(RespondToEvent("b", game.world.events.pending[0].id, accept=True))
        moods = game.agent_moods()
        game.stop_agent("a")
        await asyncio.sleep(0)
        return moods, game.agent_moods()

    moods, after_stop = asyncio.run(scenario())

    assert [(moods[i].cue, moods[i].thinking) for i in ("a", "b")] == [("happy", True)] * 2
    assert "a" not in after_stop
