from dataclasses import replace

import pytest

from aiverse_server.actions import LeaveGame, Mine, MoveTo, OfferToBuy, RespondToEvent, perform
from aiverse_server.app import Game
from aiverse_server.clock import settle_due
from aiverse_server.lifecycle import GameError, end_game
from aiverse_server.mining import MINE_SECONDS, MINE_YIELD
from aiverse_server.perception import PERCEPTION_RADIUS, begin_turn, record_activity
from aiverse_server.tokens import TOKEN
from aiverse_server.walking import WALK_SPEED, next_arrival
from aiverse_server.world import (
    ACTIVITY_LIMIT,
    Activity,
    ActivityLog,
    Inventory,
    Memory,
    Pool,
    PoolSighting,
    Position,
    Resident,
    World,
    WorldMap,
)


def resident(resident_id: str, x: float, y: float, tokens: int = 1000) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id.upper(),
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((TOKEN, tokens),)),
        coins=100,
    )


# a looks around from (10, 10): b and the near pool are in range, d right on the edge,
# c and the far pool well outside.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 10, 10),
        resident("b", 22, 10),
        resident("c", 60, 60),
        resident("d", 10, 10 + PERCEPTION_RADIUS),
    ),
    pools=(
        Pool("near", Position(20, 10), radius=4, reserve=100),
        Pool("far", Position(80, 80), radius=4, reserve=50),
    ),
    time=3.0,
)


def arrive(world: World) -> World:
    """Skip ahead until every walk has ended."""
    while (due := next_arrival(world)) is not None:
        world = settle_due(replace(world, time=due))
    return world


def test_a_resident_sees_the_pools_and_residents_in_range_and_nothing_further() -> None:
    _, seen = begin_turn(WORLD, "a")
    assert seen.time == 3.0
    assert seen.position == Position(10, 10)
    assert seen.pools == (PoolSighting("near", Position(20, 10), 4, 100, 3.0),)
    assert [other.resident_id for other in seen.residents] == ["b", "d"]
    assert seen.residents[0].name == "B"
    assert seen.residents[0].mining_pool_id is None


def test_seeing_a_pool_discovers_it_and_the_memory_outlasts_walking_away() -> None:
    world, first = begin_turn(WORLD, "a")
    assert first.discovered == ("near",)
    assert first.known_pools == first.pools
    world, second = begin_turn(world, "a")
    assert second.discovered == ()
    # b digs some tokens out while a walks off; a still remembers the reserve as it was.
    world = perform(world, Mine("b", "near"))
    world = arrive(perform(world, MoveTo("a", Position(10, 40))))
    world, away = begin_turn(world, "a")
    assert away.pools == ()
    assert away.known_pools == (PoolSighting("near", Position(20, 10), 4, 100, 3.0),)
    # Coming back updates the sighting.
    world = arrive(perform(world, MoveTo("a", Position(10, 10))))
    _, back = begin_turn(world, "a")
    sighting = PoolSighting("near", Position(20, 10), 4, 100 - MINE_YIELD, world.time)
    assert back.known_pools == (sighting,)
    assert back.discovered == ()


def test_known_pools_from_different_places_add_up() -> None:
    world, _ = begin_turn(WORLD, "a")
    world = arrive(perform(world, MoveTo("a", Position(70, 75))))
    world, seen = begin_turn(world, "a")
    assert seen.discovered == ("far",)
    assert [sighting.pool_id for sighting in seen.known_pools] == ["far", "near"]


def test_others_activity_in_range_is_noticed_once_and_own_activity_never() -> None:
    world = perform(WORLD, MoveTo("c", Position(20, 20)))  # arrives in range
    world = perform(world, MoveTo("d", Position(10, 45)))  # leaves from within range
    world = perform(world, MoveTo("b", Position(40, 40)))  # ends out of range, starts in it
    world = arrive(perform(world, MoveTo("a", Position(10, 12))))
    world, seen = begin_turn(world, "a")
    # Setting off is logged at once; arriving when each gets there, nearest first.
    assert [(entry.resident_id, entry.kind) for entry in seen.activity] == [
        ("c", "started_walking"),
        ("d", "started_walking"),
        ("b", "started_walking"),
        ("d", "moved"),
        ("b", "moved"),
        ("c", "moved"),
    ]
    start = Activity(1, 3.0, "c", "started_walking", Position(20, 20), Position(60, 60))
    assert seen.activity[0] == start
    assert seen.activity[-1].time == pytest.approx(3.0 + 40 * 2**0.5 / WALK_SPEED)
    _, again = begin_turn(world, "a")
    assert again.activity == ()


def test_activity_out_of_range_goes_unnoticed() -> None:
    world = arrive(perform(WORLD, MoveTo("c", Position(70, 70))))
    _, seen = begin_turn(world, "a")
    assert seen.activity == ()


def test_activity_right_on_the_edge_of_range_is_noticed() -> None:
    world = arrive(perform(WORLD, MoveTo("c", Position(10 + PERCEPTION_RADIUS, 10))))
    _, seen = begin_turn(world, "a")
    assert [(entry.resident_id, entry.kind) for entry in seen.activity] == [
        ("c", "started_walking"),
        ("c", "moved"),
    ]


def test_range_is_measured_from_where_the_perceiver_stands_now() -> None:
    world = perform(WORLD, MoveTo("c", Position(65, 65)))
    world = arrive(perform(world, MoveTo("a", Position(50, 50))))
    _, seen = begin_turn(world, "a")
    assert [(entry.resident_id, entry.kind) for entry in seen.activity] == [
        ("c", "started_walking"),
        ("c", "moved"),
    ]


def test_walkers_are_seen_where_they_have_got_to_on_their_way() -> None:
    # c heads straight for a; after 30 of the 70 or so units, c is still out of sight.
    world = perform(WORLD, MoveTo("c", Position(10, 10)))
    _, seen = begin_turn(replace(world, time=3.0 + 30 / WALK_SPEED), "a")
    assert [other.resident_id for other in seen.residents] == ["b", "d"]
    # Within sight later on, seen on the way with where they are heading.
    _, seen = begin_turn(replace(world, time=3.0 + 60 / WALK_SPEED), "a")
    c = next(other for other in seen.residents if other.resident_id == "c")
    assert c.walking_to == Position(10, 10)
    assert c.position.x == pytest.approx(60 - 60 / 2**0.5)
    # A walker looking around sees from where they have got to.
    _, seen = begin_turn(replace(world, time=3.0 + 60 / WALK_SPEED), "c")
    assert seen.position == c.position
    assert [sighting.pool_id for sighting in seen.pools] == ["near"]


def test_mining_is_logged_when_it_starts_and_when_the_clock_pays_it_out() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), clock=lambda: now[0])
    game.world = perform(game.world, Mine("b", "near"))
    now[0] = MINE_SECONDS
    game.tick()
    _, seen = begin_turn(game.world, "a")
    assert [(entry.kind, entry.pool_id, entry.time) for entry in seen.activity] == [
        ("started_mining", "near", 0.0),
        ("finished_mining", "near", MINE_SECONDS),
    ]


def test_a_tick_with_nothing_due_logs_nothing() -> None:
    game = Game(WORLD, clock=lambda: 1.0)
    game.tick()
    assert game.world.activity == ActivityLog()


def test_jobs_stopped_by_the_end_of_the_game_are_not_logged() -> None:
    world = perform(WORLD, Mine("b", "near"))
    now = [0.0]
    game = Game(world, duration=5.0, clock=lambda: now[0])
    now[0] = 5.0
    game.tick()
    assert game.world.ended
    assert [entry.kind for entry in game.world.activity.entries] == ["started_mining"]


def test_a_miner_who_leaves_is_logged_as_leaving_only() -> None:
    world = perform(WORLD, Mine("b", "near"))
    world = perform(world, LeaveGame("b"))
    assert [entry.kind for entry in world.activity.entries] == ["started_mining", "left_game"]


def test_a_walk_that_spends_the_last_token_logs_the_move_and_the_exit() -> None:
    world = replace(WORLD, residents=(*WORLD.residents[:3], resident("d", 10, 30, tokens=5)))
    world = arrive(perform(world, MoveTo("d", Position(10, 25))))
    kinds = [entry.kind for entry in world.activity.entries]
    assert kinds == ["started_walking", "moved", "left_game"]
    _, seen = begin_turn(world, "a")
    assert seen.residents[-1].left


def test_selling_the_last_token_logs_the_exit() -> None:
    world = replace(WORLD, residents=(WORLD.residents[0], resident("b", 22, 10, tokens=5)))
    world = perform(world, OfferToBuy("a", "b", TOKEN, 5, 10))
    world = perform(world, RespondToEvent("b", 1, accept=True))
    assert [(entry.resident_id, entry.kind) for entry in world.activity.entries] == [
        ("b", "left_game")
    ]


def test_primitives_that_change_nothing_on_the_map_log_nothing() -> None:
    world = perform(WORLD, OfferToBuy("a", "b", TOKEN, 1, 10))
    assert world.activity == ActivityLog()
    assert record_activity(world, world) is world


def test_a_busy_miner_can_still_look_around() -> None:
    world = perform(WORLD, Mine("b", "near"))
    _, seen = begin_turn(world, "b")
    assert seen.pools[0].reserve == 100 - MINE_YIELD
    _, watched = begin_turn(world, "a")
    assert watched.residents[0].mining_pool_id == "near"


@pytest.mark.parametrize(
    ("world", "resident_id", "code"),
    [
        (WORLD, "nobody", "unknown_resident"),
        (perform(WORLD, LeaveGame("a")), "a", "resident_left"),
        (end_game(WORLD), "a", "game_over"),
    ],
)
def test_looking_around_is_refused_like_acting(world: World, resident_id: str, code: str) -> None:
    with pytest.raises(GameError) as error:
        begin_turn(world, resident_id)
    assert error.value.code == code


def test_looking_around_leaves_the_rest_of_the_world_as_it_was() -> None:
    world, _ = begin_turn(WORLD, "a")
    assert world.residents[1:] == WORLD.residents[1:]
    assert replace(world.residents[0], memory=Memory()) == WORLD.residents[0]
    assert world.pools == WORLD.pools


def test_the_log_keeps_only_the_latest_entries_and_numbering_goes_on() -> None:
    world = WORLD
    # Each walk logs two entries: setting off and arriving.
    for step in range(ACTIVITY_LIMIT // 2 + 3):
        world = arrive(perform(world, MoveTo("c", Position(60, 60 + (step + 1) % 2))))
    log = world.activity
    assert len(log.entries) == ACTIVITY_LIMIT
    assert log.entries[0].seq == 7
    assert log.next_seq == ACTIVITY_LIMIT + 7


def test_a_world_rejects_memories_and_activity_it_cannot_account_for() -> None:
    a = WORLD.residents[0]
    sighting = PoolSighting("gone", Position(1, 1), 1, 1, 0.0)
    with pytest.raises(ValueError, match="remembers unknown pool"):
        replace(WORLD, residents=(replace(a, memory=Memory((sighting,))),))
    with pytest.raises(ValueError, match="not yet logged"):
        replace(WORLD, residents=(replace(a, memory=Memory(activity_seen=1)),))
    stray = Activity(1, 0.0, "ghost", "left_game", Position(1, 1))
    with pytest.raises(ValueError, match="unknown resident"):
        replace(WORLD, activity=ActivityLog((stray,), 2))
    mined = Activity(1, 0.0, "a", "started_mining", Position(1, 1), pool_id="gone")
    with pytest.raises(ValueError, match="unknown pool"):
        replace(WORLD, activity=ActivityLog((mined,), 2))
    with pytest.raises(ValueError, match="above 0"):
        ActivityLog((replace(stray, resident_id="a"),), 1)
    crowd = tuple(replace(stray, seq=seq) for seq in range(1, ACTIVITY_LIMIT + 2))
    with pytest.raises(ValueError, match="over the limit"):
        ActivityLog(crowd, ACTIVITY_LIMIT + 2)
