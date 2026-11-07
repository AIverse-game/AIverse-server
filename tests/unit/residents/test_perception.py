from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.world import (
    ACTIVITY_LIMIT,
    Activity,
    ActivityLog,
    Inventory,
    Memory,
    Position,
    Resident,
    Spring,
    SpringReserveSample,
    SpringSighting,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE, walk_cost
from aiverse_server.economy.harvesting import HARVEST_SECONDS
from aiverse_server.economy.programs import PROGRAM_TIERS
from aiverse_server.game.actions import (
    Harvest,
    LeaveGame,
    MoveTo,
    OfferToBuy,
    RespondToEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import GameError, announce, end_game
from aiverse_server.residents.memory import remember
from aiverse_server.residents.perception import PERCEPTION_RADIUS, begin_turn, record_activity
from aiverse_server.residents.walking import WALK_SPEED, next_arrival
from aiverse_server.server.app import Game

HARVEST_YIELD = PROGRAM_TIERS[0].compute_per_job
"""What a job pays with v1, the program every resident here carries."""


def resident(resident_id: str, x: float, y: float, compute: int = 1000) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id.upper(),
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((COMPUTE, compute), ("v1", 1))),
        coins=100,
    )


# a looks around from (10, 10): b and the near spring are in range, d right on the edge,
# c and the far spring well outside.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 10, 10),
        resident("b", 22, 10),
        resident("c", 60, 60),
        resident("d", 10, 10 + PERCEPTION_RADIUS),
    ),
    springs=(
        Spring("near", Position(20, 10), radius=4, reserve=100),
        Spring("far", Position(80, 80), radius=4, reserve=50),
    ),
    time=3.0,
)


def arrive(world: World) -> World:
    """Skip ahead until every walk has ended."""
    while (due := next_arrival(world)) is not None:
        world = settle_due(replace(world, time=due))
    return world


def test_a_resident_sees_the_springs_and_residents_in_range_and_nothing_further() -> None:
    _, seen = begin_turn(WORLD, "a")
    assert seen.time == 3.0
    assert seen.position == Position(10, 10)
    assert seen.springs == (SpringSighting("near", Position(20, 10), 4, 100, 3.0),)
    assert [other.resident_id for other in seen.residents] == ["b", "d"]
    assert seen.residents[0].name == "B"
    assert seen.residents[0].harvesting_spring_id is None


def test_seeing_a_spring_for_the_first_time_discovers_it() -> None:
    world, first = begin_turn(WORLD, "a")
    _, second = begin_turn(world, "a")

    assert first.discovered == ("near",)
    assert first.known_springs == first.springs
    assert second.discovered == ()


def seen_then_left() -> World:
    """a sees the near spring, then walks out of sight while b gathers some compute out of it."""
    world, _ = begin_turn(WORLD, "a")
    world = perform(world, Harvest("b", "near"))
    return arrive(perform(world, MoveTo("a", Position(10, 40))))


def test_a_spring_out_of_sight_is_remembered_as_it_was_last_seen() -> None:
    _, away = begin_turn(seen_then_left(), "a")

    assert away.springs == ()
    assert away.known_springs == (SpringSighting("near", Position(20, 10), 4, 100, 3.0),)


def test_reserve_history_refuses_a_spring_the_resident_never_saw() -> None:
    sample = SpringReserveSample("far", 3.0, 50)

    with pytest.raises(ValueError, match="unknown spring"):
        Memory(
            known_springs=(SpringSighting("near", Position(20, 10), 4, 100, 3.0),),
            spring_reserve_history=(sample,),
        )


def test_coming_back_to_a_spring_updates_the_sighting() -> None:
    world, _ = begin_turn(seen_then_left(), "a")

    world = arrive(perform(world, MoveTo("a", Position(10, 10))))
    _, back = begin_turn(world, "a")

    sighting = SpringSighting("near", Position(20, 10), 4, 100 - HARVEST_YIELD, world.time)
    assert back.known_springs == (sighting,)
    assert back.discovered == ()


def test_known_springs_from_different_places_add_up() -> None:
    world, _ = begin_turn(WORLD, "a")
    world = arrive(perform(world, MoveTo("a", Position(70, 75))))
    world, seen = begin_turn(world, "a")
    assert seen.discovered == ("far",)
    assert [sighting.spring_id for sighting in seen.known_springs] == ["far", "near"]


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
    assert [sighting.spring_id for sighting in walker.springs] == ["near"]


def test_harvesting_is_logged_when_it_starts_and_when_the_clock_pays_it_out() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), clock=lambda: now[0])
    game.world = perform(game.world, Harvest("b", "near"))
    now[0] = HARVEST_SECONDS
    game.tick()
    _, seen = begin_turn(game.world, "a")
    assert [(entry.kind, entry.spring_id, entry.time) for entry in seen.activity] == [
        ("started_harvesting", "near", 0.0),
        ("finished_harvesting", "near", HARVEST_SECONDS),
    ]


def test_a_tick_with_nothing_due_logs_nothing() -> None:
    game = Game(WORLD, clock=lambda: 1.0)
    game.tick()
    assert game.world.activity == ActivityLog()


def test_jobs_stopped_by_the_end_of_the_game_are_not_logged() -> None:
    world = perform(WORLD, Harvest("b", "near"))
    now = [0.0]
    game = Game(world, duration=5.0, clock=lambda: now[0])
    now[0] = 5.0
    game.tick()
    assert game.world.ended
    assert [entry.kind for entry in game.world.activity.entries] == ["started_harvesting"]


def test_a_harvester_who_leaves_is_logged_as_leaving_only() -> None:
    world = perform(WORLD, Harvest("b", "near"))
    world = perform(world, LeaveGame("b"))
    assert [entry.kind for entry in world.activity.entries] == ["started_harvesting", "left_game"]


def test_a_walk_that_spends_the_last_compute_logs_the_move_and_the_exit() -> None:
    world = replace(WORLD, residents=(*WORLD.residents[:3], resident("d", 10, 30, compute=3)))

    world = arrive(perform(world, MoveTo("d", Position(10, 25))))

    _, seen = begin_turn(world, "a")
    assert [entry.kind for entry in world.activity.entries] == [
        "started_walking",
        "moved",
        "left_game",
    ]
    assert seen.residents[-1].left


def test_selling_the_last_compute_logs_the_exit() -> None:
    world = replace(WORLD, residents=(WORLD.residents[0], resident("b", 22, 10, compute=5)))
    world = perform(world, OfferToBuy("a", "b", COMPUTE, 5, 10))
    world = perform(world, RespondToEvent("b", 1, accept=True))
    assert [(entry.resident_id, entry.kind) for entry in world.activity.entries] == [
        ("b", "left_game")
    ]


def test_primitives_that_change_nothing_on_the_map_log_nothing() -> None:
    world = perform(WORLD, OfferToBuy("a", "b", COMPUTE, 1, 10))
    assert world.activity == ActivityLog()
    assert record_activity(world, world) is world


def test_a_busy_harvester_can_still_look_around() -> None:
    world = perform(WORLD, Harvest("b", "near"))

    _, seen = begin_turn(world, "b")
    _, watched = begin_turn(world, "a")

    assert seen.springs[0].reserve == 100 - HARVEST_YIELD
    assert watched.residents[0].harvesting_spring_id == "near"


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
    assert world.springs == WORLD.springs


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
GONE = SpringSighting("gone", Position(1, 1), 1, 1, 0.0)
STRAY = Activity(1, 0.0, "ghost", "left_game", Position(1, 1))
HARVESTED_GONE = Activity(1, 0.0, "a", "started_harvesting", Position(1, 1), spring_id="gone")


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: replace(WORLD, residents=(replace(A, memory=Memory((GONE,))),)),
            "remembers unknown spring",
        ),
        (
            lambda: replace(WORLD, residents=(replace(A, memory=Memory(activity_seen=1)),)),
            "not yet logged",
        ),
        (lambda: Memory(found_on_walks=("near",)), "found on walks"),
        (lambda: replace(WORLD, activity=ActivityLog((STRAY,), 2)), "unknown resident"),
        (lambda: replace(WORLD, activity=ActivityLog((HARVESTED_GONE,), 2)), "unknown spring"),
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


# a walks the line x = 10 from y = 10 to y = 90. "beside" is 15 from it, more than
# PERCEPTION_RADIUS from both ends; "wide" is 30 from it; "behind" is 15 past the end.
ROAD = replace(
    WORLD,
    residents=(resident("a", 10, 10),),
    springs=(
        Spring("beside", Position(25, 50), radius=4, reserve=70),
        Spring("wide", Position(40, 50), radius=4, reserve=60),
        Spring("behind", Position(10, 99), radius=4, reserve=50),
    ),
)
ROAD_END = Position(10, 90)


def test_a_walk_discovers_the_springs_within_range_of_its_line_and_no_others() -> None:
    world = arrive(perform(ROAD, MoveTo("a", ROAD_END)))
    _, seen = begin_turn(world, "a")

    assert seen.springs == (SpringSighting("behind", Position(10, 99), 4, 50, seen.time),)
    assert [sighting.spring_id for sighting in seen.known_springs] == ["behind", "beside"]
    assert seen.discovered == ("beside", "behind")


def test_a_spring_found_on_a_walk_is_reported_once_and_remembered_as_of_arrival() -> None:
    world = arrive(perform(ROAD, MoveTo("a", ROAD_END)))
    world, first = begin_turn(world, "a")
    _, second = begin_turn(world, "a")

    beside = next(s for s in first.known_springs if s.spring_id == "beside")
    assert beside == SpringSighting("beside", Position(25, 50), 4, 70, world.time)
    assert second.discovered == ()


def test_a_resident_who_looks_mid_walk_has_seen_the_stretch_walked_so_far() -> None:
    world = perform(ROAD, MoveTo("a", ROAD_END))
    early = replace(world, time=world.time + 1.0)  # 2.5 units on: y = 12.5
    late = replace(world, time=world.time + 24.0)  # 60 units on: y = 70, 25 from "beside"

    _, early_look = begin_turn(early, "a")
    _, late_look = begin_turn(late, "a")

    assert early_look.discovered == ()
    assert late_look.springs == ()
    assert late_look.discovered == ("beside",)
    assert [sighting.spring_id for sighting in late_look.known_springs] == ["beside"]


def test_a_walk_that_only_passes_a_known_spring_discovers_nothing_new() -> None:
    world, _ = begin_turn(arrive(perform(ROAD, MoveTo("a", Position(10, 40)))), "a")

    world = arrive(perform(world, MoveTo("a", ROAD_END)))
    _, seen = begin_turn(world, "a")

    assert seen.discovered == ("behind",)


def test_passing_a_known_spring_leaves_its_sighting_as_it_was() -> None:
    world, _ = begin_turn(arrive(perform(ROAD, MoveTo("a", Position(10, 40)))), "a")
    first = next(s for s in world.residents[0].memory.known_springs if s.spring_id == "beside")
    world = replace(world, springs=(replace(world.springs[0], reserve=1), *world.springs[1:]))

    world = arrive(perform(world, MoveTo("a", ROAD_END)))
    _, seen = begin_turn(world, "a")

    assert next(s for s in seen.known_springs if s.spring_id == "beside") == first


def test_springs_found_on_walks_are_all_reported_by_the_next_look_and_only_once() -> None:
    world = arrive(perform(ROAD, MoveTo("a", Position(10, 40))))
    world, _ = remember(world, "a", "walking on", "long_term")

    world = arrive(perform(world, MoveTo("a", ROAD_END)))
    world, seen = begin_turn(world, "a")
    _, again = begin_turn(world, "a")

    assert seen.discovered == ("beside", "behind")
    assert again.discovered == ()


def test_a_walker_who_runs_out_of_compute_on_arrival_discovers_nothing() -> None:
    cost = walk_cost(Position(10, 10), ROAD_END)
    broke = replace(ROAD, residents=(resident("a", 10, 10, compute=cost),))

    world = arrive(perform(broke, MoveTo("a", ROAD_END)))

    assert world.residents[0].left
    assert world.residents[0].memory == Memory()


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
