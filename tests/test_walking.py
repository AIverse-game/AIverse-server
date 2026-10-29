from dataclasses import replace

import pytest

from aiverse_server.actions import (
    ActionRejected,
    LeaveGame,
    Mine,
    MoveTo,
    OfferToSell,
    RespondToEvent,
    perform,
)
from aiverse_server.app import Game, Outbox
from aiverse_server.clock import next_due, settle_due
from aiverse_server.inventory import item_count
from aiverse_server.lifecycle import GameError, end_game
from aiverse_server.protocol import world_view
from aiverse_server.tokens import TOKEN
from aiverse_server.walking import WALK_SPEED, position_at, walk_seconds
from aiverse_server.world import (
    Inventory,
    MiningJob,
    Pool,
    Position,
    Resident,
    WalkJob,
    World,
    WorldMap,
)


def resident(resident_id: str, x: float, y: float, tokens: int = 100) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((TOKEN, tokens), ("MK1", 1)) if tokens else ()),
        coins=100,
    )


WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 0, 0), resident("b", 50, 50)),
    pools=(Pool("p", Position(40, 0), radius=4, reserve=100),),
    time=2.0,
)

# a sets off east along the top edge: 40 units, so 40 tokens and 40 / WALK_SPEED seconds.
WALKING = perform(WORLD, MoveTo("a", Position(40, 0)))
ARRIVAL = 2.0 + 40 / WALK_SPEED


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def test_a_walk_takes_its_distance_over_the_speed_and_is_paid_when_setting_off() -> None:
    a = WALKING.residents[0]
    assert a.walking == WalkJob(Position(40, 0), departs_at=2.0, arrives_at=ARRIVAL)
    assert a.position == Position(0, 0)
    assert item_count(WALKING, "a", TOKEN) == 60
    assert walk_seconds(Position(0, 0), Position(3, 4)) == 5 / WALK_SPEED
    assert next_due(WALKING) == ARRIVAL


def test_walkers_move_steadily_and_arrive_on_time() -> None:
    a = WALKING.residents[0]
    assert position_at(a, 2.0) == Position(0, 0)
    assert position_at(a, 2.0 + 10 / WALK_SPEED) == Position(10, 0)
    assert position_at(a, ARRIVAL + 5) == Position(40, 0)
    early = replace(WALKING, time=ARRIVAL - 0.01)
    assert settle_due(early) is early
    arrived = at(WALKING, ARRIVAL).residents[0]
    assert arrived.position == Position(40, 0)
    assert arrived.walking is None


def test_standing_still_starts_no_walk() -> None:
    assert perform(WORLD, MoveTo("a", Position(0, 0))).residents[0].walking is None


@pytest.mark.parametrize("action", [MoveTo("a", Position(5, 5)), Mine("a", "p")])
def test_a_walker_can_do_nothing_but_leave(action: MoveTo | Mine) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WALKING, action)
    assert raised.value.code == "busy"
    # Once there, a can act again: mine the pool they walked to.
    assert perform(at(WALKING, ARRIVAL), Mine("a", "p")).residents[0].mining is not None


def test_leaving_on_the_way_stops_the_walker_where_they_are() -> None:
    world = perform(replace(WALKING, time=2.0 + 10 / WALK_SPEED), LeaveGame("a"))
    a = world.residents[0]
    assert a.left
    assert a.walking is None
    assert a.position == Position(10, 0)
    kinds = [entry.kind for entry in world.activity.entries]
    assert kinds == ["started_walking", "moved", "left_game"]


def test_the_end_of_the_game_stops_walkers_where_they_are() -> None:
    world = end_game(replace(WALKING, time=2.0 + 20 / WALK_SPEED))
    assert world.residents[0].position == Position(20, 0)
    assert world.residents[0].walking is None


def test_selling_the_last_tokens_on_the_way_takes_the_walker_out_on_arrival() -> None:
    world = replace(WORLD, residents=(resident("a", 0, 0, tokens=50), WORLD.residents[1]))
    world = perform(world, OfferToSell("a", "b", TOKEN, 10, 5))
    world = perform(world, MoveTo("a", Position(40, 0)))
    world = perform(world, RespondToEvent("b", 1, accept=True))
    assert not world.residents[0].left
    a = at(world, ARRIVAL).residents[0]
    assert a.left
    assert a.position == Position(40, 0)


def test_the_view_shows_walkers_on_their_way() -> None:
    view = world_view(replace(WALKING, time=2.0 + 10 / WALK_SPEED), remaining_seconds=60)
    a = view.residents[0]
    assert (a.position.x, a.position.y) == (10, 0)
    assert a.walking is not None
    assert (a.walking.target.x, a.walking.target.y) == (40, 0)
    assert a.walking.remaining_seconds == pytest.approx(30 / WALK_SPEED)
    assert view.residents[1].walking is None


def test_the_clock_brings_walkers_in_and_tells_everyone() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), clock=lambda: now[0])
    game.world = perform(game.world, MoveTo("a", Position(40, 0)))
    watcher = Outbox()
    game.watch(watcher)
    assert game._until_next() == pytest.approx(40 / WALK_SPEED)
    now[0] = 40 / WALK_SPEED
    game.tick()
    assert game.world.residents[0].position == Position(40, 0)
    assert watcher.state is not None


def test_a_walk_due_after_the_deadline_ends_where_the_walker_is_then() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), duration=4.0, clock=lambda: now[0])
    game.world = perform(game.world, MoveTo("a", Position(40, 0)))
    now[0] = 100
    game.tick()
    assert game.world.ended
    assert game.world.residents[0].position == Position(4 * WALK_SPEED, 0)


def test_beginning_a_turn_through_the_game_brings_the_world_up_to_now_first() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), clock=lambda: now[0])
    game.world = perform(game.world, MoveTo("a", Position(40, 0)))
    now[0] = 40 / WALK_SPEED
    seen = game.begin_turn("a")
    assert seen.time == 40 / WALK_SPEED
    assert seen.position == Position(40, 0)
    assert seen.discovered == ("p",)
    # What a noticed stays with the game's world.
    assert [s.pool_id for s in game.world.residents[0].memory.known_pools] == ["p"]
    with pytest.raises(GameError):
        game.begin_turn("nobody")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"left": True}, "left the game but is still walking"),
        ({"mining": MiningJob("p", 9.0, 1, "MK1")}, "walking and mining at once"),
        ({"walking": WalkJob(Position(1, 1), 5.0, 4.0)}, "ends early"),
    ],
)
def test_a_resident_rejects_impossible_walks(change: dict[str, object], message: str) -> None:
    walker = replace(WORLD.residents[0], walking=WalkJob(Position(1, 1), 0.0, 1.0))
    with pytest.raises(ValueError, match=message):
        replace(walker, **change)  # type: ignore[arg-type]


def test_a_world_rejects_walks_off_the_map_or_after_the_end() -> None:
    walker = replace(WORLD.residents[0], walking=WalkJob(Position(1, 1), 0.0, 1.0))
    with pytest.raises(ValueError, match="outside the map"):
        replace(WORLD, residents=(replace(walker, walking=WalkJob(Position(101, 0), 0.0, 1.0)),))
    with pytest.raises(ValueError, match="walking after the game ended"):
        replace(WORLD, residents=(walker,), ended=True)
