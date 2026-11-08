"""Visitors: scripted tourists who walk the map, choose ad slots to see and are counted as views."""

import math
from dataclasses import replace

import pytest

from aiverse_server.core.land import Facility, Link, Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy import visitors
from aiverse_server.economy.ads import CONTENT_LIFETIME
from aiverse_server.economy.visitors import (
    MAX_DWELL,
    MAX_STOPS,
    MIN_DWELL,
    MIN_STOPS,
    SEEN_FOR,
    TRAFFIC_REACH,
    VISITOR_COUNT,
    VISITOR_SPEED,
    Traffic,
    advance_crowd,
    new_crowd,
    next_visit,
    people_near,
    visitor_position,
    visitor_weight,
)
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.layout import create_default_world
from aiverse_server.residents.walking import WALK_SPEED


@pytest.fixture(autouse=True)
def tourists_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests here are about tourists unless they say otherwise, see `test_workers.py`."""
    monkeypatch.setattr(visitors, "WORKER_SHARE", 0.0)


ENTRANCE = Position(50, 50)
PORTAL = Facility("portal", 0.0)


def slot(col: int, row: int, pieces: int = 1) -> Parcel:
    """An ad slot showing `pieces` pieces of content that stay fresh for a long while."""
    return Parcel(col, row, "a", Facility("ad", 0.0, (CONTENT_LIFETIME * 10,) * pieces))


def world_of(*parcels: Parcel, crowd: Crowd | None = None) -> World:
    """A 100 x 100 map, so parcel L<col>-<row> is centred at (10 col + 5, 10 row + 5) and the
    entrance is (50, 50)."""
    return World(
        map=WorldMap(size=100),
        residents=(Resident("a", "a", "#123456", Position(5, 5), coins=5_000),),
        parcels=parcels,
        crowd=crowd or Crowd(),
    )


def visitor(**kwargs: object) -> Visitor:
    fields: dict[str, object] = {
        "id": 0,
        "phase": "away",
        "until": 5.0,
        "at": ENTRANCE,
        "target": ENTRANCE,
    }
    return Visitor(**{**fields, **kwargs})  # type: ignore[arg-type]


def crowd_of(*visitors: Visitor, seed: int = 0) -> Crowd:
    return Crowd(seed=seed, visitors=visitors)


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def the_visitor(world: World) -> Visitor:
    return world.crowd.visitors[0]


def test_a_visitor_comes_in_at_the_entrance_and_heads_for_the_slot_worth_seeing() -> None:
    world = world_of(slot(5, 5), crowd=crowd_of(visitor()))

    after = at(world, 5.0)

    walker = the_visitor(after)
    assert (walker.phase, walker.goal, walker.stop) == ("walking", "place", "L5-5")
    assert walker.at == ENTRANCE
    assert walker.target == Position(55, 55)
    assert walker.until == 5.0 + math.dist((50, 50), (55, 55)) / VISITOR_SPEED
    assert MIN_STOPS <= walker.stops_left <= MAX_STOPS


def test_a_visitor_who_stays_at_a_slot_is_counted_once_with_the_people_it_stands_for() -> None:
    world = world_of(slot(5, 5), crowd=crowd_of(visitor()))
    walking = the_visitor(at(world, 5.0))

    arrived = at(at(world, 5.0), walking.until)

    stayer = the_visitor(arrived)
    assert stayer.phase == "staying"
    assert MIN_DWELL <= stayer.until - walking.until <= MAX_DWELL
    assert arrived.crowd.views == (("L5-5", visitor_weight(walking.until)),)
    assert stayer.seen == (("L5-5", walking.until + SEEN_FOR),)


def test_a_visitor_does_not_come_back_to_a_slot_it_was_just_counted_at() -> None:
    world = world_of(slot(5, 5), crowd=crowd_of(visitor(), seed=3))
    stayer = the_visitor(at(at(world, 5.0), the_visitor(at(world, 5.0)).until))

    done = at(at(at(world, 5.0), the_visitor(at(world, 5.0)).until), stayer.until)

    assert the_visitor(done).goal == "wander"
    assert done.crowd.views == (("L5-5", visitor_weight(the_visitor(at(world, 5.0)).until)),)


def test_a_visitor_who_has_seen_enough_walks_out_and_stays_away_before_coming_again() -> None:
    far = Position(80, 80)
    world = world_of(
        crowd=crowd_of(
            visitor(phase="staying", until=20.0, at=far, target=far, goal="exit", stops_left=0)
        )
    )

    leaving = at(world, 20.0)
    gone = at(leaving, the_visitor(leaving).until)
    back = at(gone, the_visitor(gone).until)

    assert (the_visitor(leaving).phase, the_visitor(leaving).goal) == ("walking", "exit")
    assert the_visitor(leaving).target == ENTRANCE
    assert the_visitor(gone).phase == "away"
    assert the_visitor(gone).until > the_visitor(leaving).until
    assert visitor_position(the_visitor(gone), the_visitor(gone).until) is None
    assert the_visitor(back).trip == 1


def test_a_visitor_passing_by_a_slot_sees_it_without_stopping() -> None:
    walk = visitor(
        phase="walking",
        until=40.0 / VISITOR_SPEED,
        at=Position(30, 50),
        target=Position(70, 50),
        goal="exit",
    )
    # L5-4 is centred at (55, 45), 5 off the line, L5-9 at (55, 95), far from it.
    world = world_of(slot(5, 4), slot(5, 9), crowd=crowd_of(walk))

    arrived = at(world, walk.until)

    assert dict(arrived.crowd.views).keys() == {"L5-4"}


def test_a_visitor_who_only_clips_the_edge_of_a_slots_circle_does_not_see_it() -> None:
    walk = visitor(
        phase="walking",
        until=40.0 / VISITOR_SPEED,
        at=Position(30, 50),
        target=Position(70, 50),
        goal="exit",
    )
    # L5-3 is centred at (55, 35), 15 off the line and out of reach.
    world = world_of(slot(5, 3), crowd=crowd_of(walk))

    arrived = at(world, walk.until)

    assert arrived.crowd.views == ()


def test_a_slot_showing_nothing_is_neither_chosen_nor_seen() -> None:
    bare = Parcel(5, 5, "a", Facility("ad", 0.0))
    world = world_of(bare, crowd=crowd_of(visitor()))

    after = at(world, 5.0)

    assert the_visitor(after).goal == "wander"
    assert the_visitor(after).stop is None


def test_only_ad_slots_are_chosen() -> None:
    hall = Parcel(5, 5, "a", Facility("game_hall", 0.0, (CONTENT_LIFETIME * 10,)))
    world = world_of(hall, crowd=crowd_of(visitor()))

    after = at(world, 5.0)

    assert the_visitor(after).goal == "wander"


def first_picks(*slots: Parcel) -> list[str | None]:
    """Where each of 100 visitors, differing only in the seed of their dice, heads first."""
    picks = []
    for seed in range(100):
        world = world_of(*slots, crowd=crowd_of(visitor(), seed=seed))
        picks.append(the_visitor(at(world, 5.0)).stop)
    return picks


def test_a_slot_with_more_content_draws_more_visitors_all_else_equal() -> None:
    # L2-5 and L7-5 are centred 25 either side of the entrance.
    picks = first_picks(slot(2, 5, pieces=5), slot(7, 5, pieces=1))

    assert picks.count("L2-5") > picks.count("L7-5") > 0


def test_nearer_slots_draw_more_visitors_all_else_equal() -> None:
    picks = first_picks(slot(5, 5, pieces=3), slot(0, 0, pieces=3))

    assert picks.count("L5-5") > picks.count("L0-0") > 0


def test_a_crowded_slot_loses_visitors_to_a_worse_one() -> None:
    # Everyone comes in together, so the slots fill up while they are chosen. Alone, most would
    # pick the five pieces over the one.
    packed = tuple(visitor(id=i, until=5.0) for i in range(40))
    world = world_of(slot(2, 5, pieces=5), slot(7, 5, pieces=1), crowd=crowd_of(*packed, seed=4))

    after = at(world, 5.0)

    better = sum(v.stop == "L2-5" for v in after.crowd.visitors)
    alone = first_picks(slot(2, 5, pieces=5), slot(7, 5, pieces=1)).count("L2-5")
    assert better / 40 < alone / 100 - 0.1


def test_a_slot_stops_counting_the_visitors_who_have_left_it() -> None:
    # Thirty visitors finish their stay at L2-5, with nowhere left to go, in the same moment as
    # one more chooses between it and L7-5, which are as good as each other and as near.
    both = (("L2-5", 999.0), ("L7-5", 999.0))
    leaving = tuple(
        visitor(
            id=i, phase="staying", until=5.0, goal="place", stop="L2-5", stops_left=1, seen=both
        )
        for i in range(30)
    )
    picks = []
    for seed in range(60):
        chooser = visitor(id=99, until=5.0, stops_left=2)
        crowd = crowd_of(*leaving, chooser, seed=seed)
        world = world_of(slot(2, 5, pieces=2), slot(7, 5, pieces=2), crowd=crowd)
        after = at(world, 5.0)
        picks.append(after.crowd.visitors[-1].stop)

    assert picks.count("L2-5") > 10


def test_a_slot_a_visitor_was_just_counted_at_is_not_counted_again_when_it_passes_by() -> None:
    walk = visitor(
        phase="walking",
        until=40.0 / VISITOR_SPEED,
        at=Position(30, 50),
        target=Position(70, 50),
        goal="exit",
        seen=(("L5-4", 500.0),),
    )
    world = world_of(slot(5, 4), crowd=crowd_of(walk))

    arrived = at(world, walk.until)

    assert arrived.crowd.views == ()


def test_a_visitor_back_on_a_new_trip_is_not_counted_again_at_a_slot_it_was_counted_at() -> None:
    # Away until 5, having been counted at L5-5 until 100, with a trip that wants one stop.
    back = visitor(seen=(("L5-5", 100.0),))
    world = world_of(slot(5, 5), crowd=crowd_of(back))

    after = at(world, 5.0)

    assert the_visitor(after).goal in ("wander", "exit")
    assert the_visitor(after).stop is None
    assert after.crowd.views == ()


def test_a_visitor_with_nothing_to_see_gives_up_roaming_and_leaves_in_the_end() -> None:
    world = world_of(crowd=crowd_of(visitor(), seed=9))

    after = at(world, 1_200.0)

    assert the_visitor(after).trip > 1


def test_the_crowd_plays_out_the_same_way_from_the_same_seed_and_not_from_another() -> None:
    world = world_of(slot(2, 5, 2), slot(7, 5, 2), slot(5, 8, 2), crowd=new_crowd(30, seed=7))
    other = replace(world, crowd=new_crowd(30, seed=8))

    first, again, different = at(world, 300.0), at(world, 300.0), at(other, 300.0)

    assert first.crowd == again.crowd
    assert first.crowd.views != different.crowd.views


def test_visitors_roam_when_nothing_is_worth_seeing_and_stay_on_the_map() -> None:
    world = world_of(crowd=new_crowd(20, seed=5))

    after = at(world, 600.0)

    positions = [visitor_position(v, 600.0) for v in after.crowd.visitors]
    assert any(p is not None for p in positions)
    assert all(p is None or (0 <= p.x <= 100 and 0 <= p.y <= 100) for p in positions)
    assert after.crowd.views == ()


def test_each_view_adds_more_people_as_the_game_goes_on() -> None:
    assert visitor_weight(0.0) == 1.0
    assert visitor_weight(1_200.0) == 2.0
    world = world_of(slot(5, 5), crowd=crowd_of(visitor(until=1_200.0)))
    walking = the_visitor(replace(at(replace(world, time=1_200.0), 1_200.0)))

    arrived = at(at(replace(world, time=1_200.0), 1_200.0), walking.until)

    assert arrived.crowd.views == (("L5-5", visitor_weight(walking.until)),)
    assert arrived.crowd.views[0][1] > 2.0


def test_a_walker_is_where_the_line_from_start_to_target_puts_them() -> None:
    walk = visitor(
        phase="walking",
        until=10.0 + 40.0 / VISITOR_SPEED,
        at=Position(30, 50),
        target=Position(70, 50),
        departs_at=10.0,
        goal="exit",
    )

    before = visitor_position(walk, 5.0)
    midway = visitor_position(walk, 10.0 + 20.0 / VISITOR_SPEED)
    after = visitor_position(walk, walk.until + 100)

    assert before == Position(30, 50)
    assert midway == Position(50, 50)
    assert after == Position(70, 50)


def test_a_visitor_takes_a_network_of_portals_when_it_is_shorter_and_comes_out_at_once() -> None:
    # Portals on L0-5 (5, 55) and L9-5 (95, 55), joined; the slot on L8-5 (85, 55) is 70 from
    # the visitor at (10, 55) on foot but only 5 + 10 through the portals.
    portals = (Parcel(0, 5, "a", PORTAL), Parcel(9, 5, "a", PORTAL))
    base = replace(world_of(*portals, slot(8, 5)), links=(Link("L0-5", "L9-5", 0),))
    walk = visitor(
        phase="staying", until=1.0, at=Position(10, 55), target=Position(10, 55), stops_left=2
    )
    world = replace(base, crowd=crowd_of(walk))

    after = at(world, 1.0)

    walker = the_visitor(after)
    assert walker.via == (Position(5, 55), Position(95, 55))
    assert walker.until == 1.0 + (5 + 10) / VISITOR_SPEED
    # Once through, it is at the far portal and walks on from there.
    assert visitor_position(walker, walker.until) == Position(85, 55)


def test_portals_that_are_not_joined_save_the_visitor_nothing() -> None:
    portals = (Parcel(0, 5, "a", PORTAL), Parcel(9, 5, "a", PORTAL))
    walk = visitor(
        phase="staying", until=1.0, at=Position(10, 55), target=Position(10, 55), stops_left=2
    )
    world = world_of(*portals, slot(8, 5), crowd=crowd_of(walk))

    after = at(world, 1.0)

    assert the_visitor(after).via is None
    assert the_visitor(after).until == 1.0 + 75 / VISITOR_SPEED


def test_a_visitor_walking_through_portals_sees_the_slots_on_the_stretches_it_walks() -> None:
    # The slot on L1-5 (15, 55) lies on the first stretch, from (30, 55) to the portal at
    # (5, 55); the one on L5-5 (55, 55) on the stretch skipped through the portals, which is
    # not walked.
    portals = (Parcel(0, 5, "a", PORTAL), Parcel(9, 5, "a", PORTAL))
    base = replace(world_of(*portals, slot(1, 5), slot(5, 5)), links=(Link("L0-5", "L9-5", 0),))
    walk = visitor(
        phase="walking",
        until=(25.0 + 10.0) / VISITOR_SPEED,
        at=Position(30, 55),
        target=Position(85, 55),
        via=(Position(5, 55), Position(95, 55)),
        goal="exit",
    )
    world = replace(base, crowd=crowd_of(walk))

    arrived = at(world, walk.until)

    assert dict(arrived.crowd.views).keys() == {"L1-5"}


def test_the_clock_waits_for_the_next_step_of_a_visitor() -> None:
    world = world_of(crowd=crowd_of(visitor(until=12.0), visitor(id=1, until=7.0)))

    assert next_visit(world) == 7.0
    assert next_due(world) == 7.0
    assert next_due(world_of()) is None


def test_nothing_changes_before_a_visitors_step_is_due() -> None:
    world = world_of(slot(5, 5), crowd=crowd_of(visitor(until=12.0)))

    assert advance_crowd(replace(world, time=11.0)) == replace(world, time=11.0)


def test_the_default_world_has_a_crowd_that_has_not_come_in_yet() -> None:
    world = create_default_world()

    assert len(world.crowd.visitors) == VISITOR_COUNT
    assert all(v.phase == "away" for v in world.crowd.visitors)
    assert all(0 < v.until <= 120.0 for v in world.crowd.visitors)


def test_the_people_passing_a_spot_are_the_visitors_on_the_map_within_reach() -> None:
    near = visitor(id=0, phase="staying", until=99.0, at=Position(50, 50), target=Position(50, 50))
    edge = replace(near, id=1, at=Position(50 + TRAFFIC_REACH, 50), target=Position(70, 50))
    far = replace(near, id=2, at=Position(50 + TRAFFIC_REACH + 1, 50), target=Position(71, 50))
    gone = visitor(id=3, until=99.0)
    world = world_of(crowd=crowd_of(near, edge, far, gone))

    assert people_near(world, Position(50, 50)) == 2.0
    assert people_near(replace(world, time=1_200.0), Position(50, 50)) == 2 * 2.0


def test_traffic_counts_visitors_in_the_next_square_over_and_answers_for_many_spots() -> None:
    across = visitor(phase="staying", until=99.0, at=Position(39.9, 30), target=Position(39.9, 30))
    world = world_of(crowd=crowd_of(across))

    traffic = Traffic(world)

    # The visitor lies in the square 20..40 and the spot in 40..60, 20.1 apart in x ...
    assert traffic.near(Position(40.1, 30)) == 1.0
    assert traffic.near(Position(60.5, 30)) == 0.0
    assert traffic.near(Position(40.1, 30)) == people_near(world, Position(40.1, 30))


def test_visitors_walk_as_fast_as_residents() -> None:
    assert VISITOR_SPEED == WALK_SPEED
