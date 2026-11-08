"""Working tourists: tourists whose rough route is a workspace and then a game hall, paying the
system-set fee at each door; in nothing else they differ from the others."""

from dataclasses import replace

import pytest

from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy import visitors
from aiverse_server.economy.admissions import FEES
from aiverse_server.economy.ads import CONTENT_LIFETIME
from aiverse_server.economy.visitors import DWELL, VISITOR_SPEED, new_crowd, visitor_weight
from aiverse_server.game.clock import settle_due

ENTRANCE = Position(50, 50)
WORKSPACE = Facility("workspace", 0.0)
HALL = Facility("game_hall", 0.0, (CONTENT_LIFETIME * 10,) * 2)


@pytest.fixture(autouse=True)
def only_working(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visitors, "ROUTE_SHARE", 1.0)


def world_of(*parcels: Parcel, crowd: Crowd | None = None, time: float = 0.0) -> World:
    """A 100 x 100 map: parcel L<col>-<row> is centred at (10 col + 5, 10 row + 5)."""
    return World(
        map=WorldMap(size=100),
        residents=(Resident("a", "a", "#123456", Position(5, 5), coins=1_000),),
        parcels=parcels,
        crowd=crowd or Crowd(),
        time=time,
    )


def arriving(until: float = 5.0, seed: int = 0) -> Crowd:
    """One visitor coming in at `until`."""
    return Crowd(seed=seed, visitors=(Visitor(0, "away", until, ENTRANCE, ENTRANCE),))


def the_visitor(world: World) -> Visitor:
    return world.crowd.visitors[0]


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def taken(world: World) -> float:
    """Coins of fees taken at the doors so far: paid out and still to be."""
    paid = sum(t.amount for t in world.ledger.transfers if t.channel == "visitors")
    return paid + sum(coins for _, coins in world.admissions.unpaid)


def test_a_visitor_on_the_working_route_heads_for_a_workspace_first() -> None:
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving())

    after = at(world, 5.0)

    walker = the_visitor(after)
    assert (walker.goal, walker.stop) == ("place", "L5-5")
    assert walker.route == ("game_hall",)
    assert walker.stops_left == 0


def test_the_fee_is_paid_for_all_the_people_the_visitor_stands_for_when_it_goes_in() -> None:
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving())
    walking = the_visitor(at(world, 5.0))

    inside = at(at(world, 5.0), walking.until)

    stayer = the_visitor(inside)
    people = visitor_weight(walking.until)
    low, high = DWELL["workspace"]
    assert stayer.phase == "staying" and low <= stayer.until - walking.until <= high
    assert dict(inside.admissions.unpaid) == {"L5-5": pytest.approx(FEES["workspace"] * people)}
    assert dict(inside.admissions.admitted) == {"L5-5": pytest.approx(people)}


def test_the_fee_grows_with_the_people_each_visitor_stands_for() -> None:
    early = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving(until=5.0))
    late = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving(until=1_205.0), time=1_200.0)

    early_arrives = the_visitor(at(early, 5.0)).until
    early_in = at(at(early, 5.0), early_arrives)
    late_in = at(at(late, 1_205.0), the_visitor(at(late, 1_205.0)).until)

    assert taken(early_in) == pytest.approx(FEES["workspace"] * visitor_weight(early_arrives))
    assert taken(late_in) > 1.9 * taken(early_in)


def test_after_the_workspace_the_visitor_goes_to_a_game_hall_and_pays_there() -> None:
    working = Visitor(0, "staying", 10.0, ENTRANCE, ENTRANCE, route=("game_hall",))
    world = world_of(
        Parcel(5, 5, "a", WORKSPACE), Parcel(2, 5, "a", HALL), crowd=Crowd(visitors=(working,))
    )
    walking = the_visitor(at(world, 10.0))

    played = at(at(world, 10.0), walking.until)

    assert (walking.goal, walking.stop, walking.route) == ("place", "L2-5", ())
    assert set(dict(played.admissions.unpaid)) == {"L2-5"}
    assert taken(played) == pytest.approx(FEES["game_hall"] * visitor_weight(walking.until))


def test_after_the_last_stop_on_the_route_the_visitor_leaves_without_roaming() -> None:
    done = Visitor(0, "staying", 10.0, ENTRANCE, ENTRANCE, route=())
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=Crowd(visitors=(done,)))

    after = at(world, 10.0)

    assert (the_visitor(after).goal, the_visitor(after).stop) == ("exit", None)


def test_a_game_hall_that_shows_no_games_and_a_workspace_not_built_are_skipped() -> None:
    bare_hall = Facility("game_hall", 0.0)
    world = world_of(Parcel(2, 5, "a", bare_hall), crowd=arriving())

    after = at(world, 5.0)

    assert the_visitor(after).goal == "exit"
    assert the_visitor(after).route == ()


def test_the_visitor_goes_straight_to_the_game_hall_when_there_is_no_workspace() -> None:
    world = world_of(Parcel(2, 5, "a", HALL), crowd=arriving(seed=1))

    after = at(world, 5.0)

    assert (the_visitor(after).goal, the_visitor(after).stop) == ("place", "L2-5")


def test_a_visitor_with_nowhere_on_its_route_to_go_leaves_and_does_not_roam() -> None:
    world = world_of(crowd=arriving())

    after = at(world, 5.0)

    assert the_visitor(after).goal == "exit"


def test_a_visitor_on_the_route_does_not_go_to_ad_slots_but_counts_as_seeing_one_it_passes() -> (
    None
):
    # The ad slot on L7-5 (75, 55) lies close to the line from the entrance to the workspace on
    # L9-5 (95, 55), so a visitor walking there passes within sight of it.
    on_the_way = Parcel(7, 5, "a", Facility("ad", 0.0, (CONTENT_LIFETIME * 10,)))
    world = world_of(on_the_way, Parcel(9, 5, "a", WORKSPACE), crowd=arriving())
    walking = the_visitor(at(world, 5.0))

    arrived = at(at(world, 5.0), walking.until)

    assert walking.stop == "L9-5"
    assert dict(arrived.crowd.views).keys() == {"L7-5"}
    assert dict(arrived.admissions.unpaid).keys() == {"L9-5"}


def test_a_visitor_off_the_route_goes_to_a_venue_and_pays_the_fee_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visitors, "ROUTE_SHARE", 0.0)
    world = world_of(Parcel(2, 5, "a", WORKSPACE), Parcel(2, 6, "a", HALL), crowd=arriving())

    after = at(world, 60.0)

    assert taken(after) > 0
    assert set(dict(after.admissions.admitted)) <= {"L2-5", "L2-6"}


def test_the_fees_taken_are_the_fee_times_the_people_let_in() -> None:
    world = world_of(
        Parcel(5, 5, "a", WORKSPACE), Parcel(2, 5, "a", HALL), crowd=new_crowd(80, seed=4)
    )

    after = at(world, 900.0)

    for parcel_id, kind in (("L5-5", "workspace"), ("L2-5", "game_hall")):
        assert dict(after.admissions.unpaid).get(parcel_id, 0.0) <= (
            FEES[kind] * dict(after.admissions.admitted).get(parcel_id, 0.0) + 1e-6
        )


def on_the_route(monkeypatch: pytest.MonkeyPatch, share: float) -> tuple[int, int]:
    """Visitors out in town at 125 s, and those of them still on a working route, with `share` of
    the trips set to follow the route."""
    monkeypatch.setattr(visitors, "ROUTE_SHARE", share)
    world = world_of(
        Parcel(5, 5, "a", WORKSPACE), Parcel(2, 5, "a", HALL), crowd=new_crowd(200, seed=5)
    )

    after = at(world, 125.0)

    out = [v for v in after.crowd.visitors if v.trip and v.phase != "away"]
    return len([v for v in out if v.route]), len(out)


def test_a_share_of_the_trips_follow_the_working_route_when_the_dice_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    none, _ = on_the_route(monkeypatch, 0.0)

    some, out = on_the_route(monkeypatch, 0.3)

    # Those who have already been through both doors are no longer marked, so the share seen
    # is lower than the 30% of the trips.
    assert none == 0
    assert 0.1 < some / out < 0.35


def test_a_walk_to_a_workspace_is_the_speed_of_a_walk() -> None:
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving())

    walking = the_visitor(at(world, 5.0))

    assert walking.until == pytest.approx(5.0 + 7.0710678 / VISITOR_SPEED)
