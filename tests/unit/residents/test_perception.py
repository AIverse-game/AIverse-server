from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.world import (
    ACTIVITY_LIMIT,
    Activity,
    ActivityLog,
    Memory,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.game.actions import LeaveGame, MoveTo, OfferToBuy, perform
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import GameError, announce, end_game
from aiverse_server.residents.perception import PERCEPTION_RADIUS, begin_turn, record_activity
from aiverse_server.residents.walking import WALK_SPEED, next_arrival
from aiverse_server.server.app import Game


def resident(resident_id: str, x: float, y: float) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id.upper(),
        color="#123456",
        position=Position(x, y),
        coins=100,
    )


# a looks around from (10, 10): b is in range, d right on the edge, c well outside.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 10, 10),
        resident("b", 22, 10),
        resident("c", 60, 60),
        resident("d", 10, 10 + PERCEPTION_RADIUS),
    ),
    time=3.0,
)


def arrive(world: World) -> World:
    """Skip ahead until every walk has ended."""
    while (due := next_arrival(world)) is not None:
        world = settle_due(replace(world, time=due))
    return world


def test_a_resident_sees_the_residents_in_range_and_nobody_further() -> None:
    _, seen = begin_turn(WORLD, "a")

    assert seen.time == 3.0
    assert seen.position == Position(10, 10)
    assert [other.resident_id for other in seen.residents] == ["b", "d"]
    assert seen.residents[0].name == "B"
    assert seen.residents[0].walking_to is None


def test_others_activity_in_range_is_noticed_once_and_own_activity_never() -> None:
    world = perform(WORLD, MoveTo("c", Position(20, 20)))  # arrives in range
    world = perform(world, MoveTo("d", Position(10, 45)))  # leaves from within range
    world = perform(world, MoveTo("b", Position(40, 40)))  # ends out of range, starts in it
    world = arrive(perform(world, MoveTo("a", Position(10, 12))))

    world, seen = begin_turn(world, "a")
    _, again = begin_turn(world, "a")

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

    _, early = begin_turn(replace(world, time=3.0 + 30 / WALK_SPEED), "a")
    _, later = begin_turn(replace(world, time=3.0 + 60 / WALK_SPEED), "a")
    _, walker = begin_turn(replace(world, time=3.0 + 60 / WALK_SPEED), "c")

    assert [other.resident_id for other in early.residents] == ["b", "d"]
    # Within sight later on, seen on the way with where they are heading.
    c = next(other for other in later.residents if other.resident_id == "c")
    assert c.walking_to == Position(10, 10)
    assert c.position.x == pytest.approx(60 - 60 / 2**0.5)
    # A walker looking around sees from where they have got to.
    assert walker.position == c.position


def test_a_tick_with_nothing_due_logs_nothing() -> None:
    game = Game(WORLD, clock=lambda: 1.0)
    game.tick()
    assert game.world.activity == ActivityLog()


def test_a_walker_leaving_on_the_way_is_logged_moving_and_leaving() -> None:
    world = perform(WORLD, MoveTo("d", Position(10, 45)))
    world = perform(replace(world, time=world.time + 4.0), LeaveGame("d"))

    assert [entry.kind for entry in world.activity.entries] == [
        "started_walking",
        "moved",
        "left_game",
    ]


def test_primitives_that_change_nothing_on_the_map_log_nothing() -> None:
    world = perform(WORLD, OfferToBuy("a", "b", "poster", 1, 10))
    assert world.activity == ActivityLog()
    assert record_activity(world, world) is world


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


def test_the_log_keeps_only_the_latest_entries_and_numbering_goes_on() -> None:
    world = WORLD
    # Each walk logs two entries: setting off and arriving.
    for step in range(ACTIVITY_LIMIT // 2 + 3):
        world = arrive(perform(world, MoveTo("c", Position(60, 60 + (step + 1) % 2))))
    log = world.activity
    assert len(log.entries) == ACTIVITY_LIMIT
    assert log.entries[0].seq == 7
    assert log.next_seq == ACTIVITY_LIMIT + 7


A = WORLD.residents[0]
STRAY = Activity(1, 0.0, "ghost", "left_game", Position(1, 1))


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: replace(WORLD, residents=(replace(A, memory=Memory(activity_seen=1)),)),
            "not yet logged",
        ),
        (lambda: replace(WORLD, activity=ActivityLog((STRAY,), 2)), "unknown resident"),
        (lambda: ActivityLog((replace(STRAY, resident_id="a"),), 1), "above 0"),
        (
            lambda: ActivityLog(
                tuple(replace(STRAY, seq=seq) for seq in range(1, ACTIVITY_LIMIT + 2)),
                ACTIVITY_LIMIT + 2,
            ),
            "over the limit",
        ),
    ],
)
def test_a_world_rejects_memories_and_activity_it_cannot_account_for(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_a_look_reads_each_notice_once_wherever_the_resident_stands() -> None:
    world = announce(WORLD, "the first")

    world, first = begin_turn(world, "a")
    world = announce(world, "the second")
    world, second = begin_turn(world, "a")
    _, third = begin_turn(world, "a")

    assert [notice.text for notice in first.notices] == ["the first"]
    assert [notice.text for notice in second.notices] == ["the second"]
    assert third.notices == ()


def test_a_notice_waits_for_a_resident_who_has_not_looked_yet() -> None:
    world = announce(WORLD, "the first")
    world, _ = begin_turn(world, "a")

    _, perception = begin_turn(world, "b")

    assert [notice.text for notice in perception.notices] == ["the first"]
