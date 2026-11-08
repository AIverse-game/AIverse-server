"""Workers: visitors who go to a workspace and then a game hall, paying a fee at each door."""

from dataclasses import replace

import pytest

from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy import visitors
from aiverse_server.economy.admissions import FEES
from aiverse_server.economy.ads import CONTENT_LIFETIME
from aiverse_server.economy.visitors import (
    DWELL,
    MAX_PURSE,
    MIN_PURSE,
    VISITOR_SPEED,
    new_crowd,
    visitor_weight,
)
from aiverse_server.game.clock import settle_due

ENTRANCE = Position(50, 50)
WORKSPACE = Facility("workspace", 0.0)
HALL = Facility("game_hall", 0.0, (CONTENT_LIFETIME * 10,) * 2)


@pytest.fixture(autouse=True)
def only_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visitors, "WORKER_SHARE", 1.0)


def world_of(*parcels: Parcel, crowd: Crowd | None = None, time: float = 0.0) -> World:
    """A 100 x 100 map: parcel L<col>-<row> is centred at (10 col + 5, 10 row + 5)."""
    return World(
        map=WorldMap(size=100),
        residents=(Resident("a", "a", "#123456", Position(5, 5), coins=1_000),),
        parcels=parcels,
        crowd=crowd or Crowd(),
        time=time,
    )


def arriving(purse: float = 0.0, until: float = 5.0, seed: int = 0, **kwargs: object) -> Crowd:
    """One visitor coming in at `until`; the dice decide its purse unless `purse` is given."""
    coming = Visitor(0, "away", until, ENTRANCE, ENTRANCE)
    return Crowd(seed=seed, visitors=(replace(coming, **kwargs),))  # type: ignore[arg-type]


def the_visitor(world: World) -> Visitor:
    return world.crowd.visitors[0]


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def test_a_visitor_comes_in_as_a_worker_with_a_purse_and_heads_for_a_workspace_first() -> None:
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving())

    after = at(world, 5.0)

    worker = the_visitor(after)
    assert (worker.kind, worker.goal, worker.stop) == ("worker", "place", "L5-5")
    assert worker.errands == ("game_hall",)
    assert MIN_PURSE <= worker.purse <= MAX_PURSE
    assert worker.stops_left == 0


def test_a_worker_pays_the_fee_for_all_the_people_it_stands_for_when_it_goes_in() -> None:
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving(), time=0.0)
    walking = the_visitor(at(world, 5.0))

    inside = at(at(world, 5.0), walking.until)

    stayer = the_visitor(inside)
    people = visitor_weight(walking.until)
    low, high = DWELL["workspace"]
    assert stayer.phase == "staying" and low <= stayer.until - walking.until <= high
    assert stayer.purse == pytest.approx(walking.purse - FEES["workspace"])
    assert dict(inside.admissions.unpaid) == {"L5-5": pytest.approx(FEES["workspace"] * people)}
    assert dict(inside.admissions.admitted) == {"L5-5": pytest.approx(people)}


def taken(world: World) -> float:
    """Coins of fees taken at the doors so far: paid out and still to be."""
    paid = sum(t.amount for t in world.ledger.transfers if t.channel == "visitors")
    return paid + sum(coins for _, coins in world.admissions.unpaid)


def test_the_fee_grows_with_the_people_each_visitor_stands_for() -> None:
    early = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving(until=5.0))
    late = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving(until=1_205.0), time=1_200.0)

    early_arrives = the_visitor(at(early, 5.0)).until
    early_in = at(at(early, 5.0), early_arrives)
    late_in = at(at(late, 1_205.0), the_visitor(at(late, 1_205.0)).until)

    assert taken(early_in) == pytest.approx(FEES["workspace"] * visitor_weight(early_arrives))
    assert taken(late_in) > 1.9 * taken(early_in)


def test_after_the_workspace_a_worker_with_money_left_goes_to_a_game_hall_and_pays_there() -> None:
    working = Visitor(
        0, "staying", 10.0, ENTRANCE, ENTRANCE, kind="worker", errands=("game_hall",), purse=10.0
    )
    world = world_of(
        Parcel(5, 5, "a", WORKSPACE), Parcel(2, 5, "a", HALL), crowd=Crowd(visitors=(working,))
    )
    walking = the_visitor(at(world, 10.0))

    played = at(at(world, 10.0), walking.until)

    assert (walking.goal, walking.stop, walking.errands) == ("place", "L2-5", ())
    assert the_visitor(played).purse == pytest.approx(10.0 - FEES["game_hall"])
    assert set(dict(played.admissions.unpaid)) == {"L2-5"}


def test_a_worker_who_cannot_afford_the_game_hall_goes_home_without_trying() -> None:
    poor = Visitor(
        0, "staying", 10.0, ENTRANCE, ENTRANCE, kind="worker", errands=("game_hall",), purse=5.0
    )
    world = world_of(Parcel(2, 5, "a", HALL), crowd=Crowd(visitors=(poor,)))

    after = at(world, 10.0)

    assert the_visitor(after).goal == "exit"
    assert after.admissions.unpaid == ()


def test_a_worker_skips_a_game_hall_that_shows_no_games_and_a_workspace_that_is_not_built() -> None:
    bare_hall = Facility("game_hall", 0.0)
    world = world_of(Parcel(2, 5, "a", bare_hall), crowd=arriving())

    after = at(world, 5.0)

    assert the_visitor(after).goal == "exit"
    assert the_visitor(after).errands == ()


def test_a_worker_goes_straight_to_the_game_hall_when_there_is_no_workspace() -> None:
    world = world_of(Parcel(2, 5, "a", HALL), crowd=arriving(seed=1))

    after = at(world, 5.0)

    assert (the_visitor(after).goal, the_visitor(after).stop) == ("place", "L2-5")


def test_a_worker_with_nothing_to_go_to_leaves_and_does_not_roam() -> None:
    world = world_of(crowd=arriving())

    after = at(world, 5.0)

    assert the_visitor(after).goal == "exit"


def test_a_worker_does_not_go_to_ad_slots_but_is_counted_as_having_seen_one_it_passes() -> None:
    # The ad slot on L7-5 (75, 55) lies close to the line from the entrance to the workspace on
    # L9-5 (95, 55), so a worker walking there passes within sight of it.
    on_the_way = Parcel(7, 5, "a", Facility("ad", 0.0, (CONTENT_LIFETIME * 10,)))
    world = world_of(on_the_way, Parcel(9, 5, "a", WORKSPACE), crowd=arriving())
    walking = the_visitor(at(world, 5.0))

    arrived = at(at(world, 5.0), walking.until)

    assert walking.stop == "L9-5"
    assert dict(arrived.crowd.views).keys() == {"L7-5"}
    assert dict(arrived.admissions.unpaid).keys() == {"L9-5"}


def test_a_tourist_looks_at_ad_slots_and_never_pays_a_fee(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visitors, "WORKER_SHARE", 0.0)
    slot = Parcel(5, 5, "a", Facility("ad", 0.0, (CONTENT_LIFETIME * 10,)))
    world = world_of(slot, Parcel(2, 5, "a", WORKSPACE), Parcel(2, 6, "a", HALL), crowd=arriving())

    after = at(world, 60.0)

    assert after.crowd.views != ()
    assert after.admissions.unpaid == ()


def test_a_fee_more_than_the_purse_is_never_taken() -> None:
    world = world_of(
        Parcel(5, 5, "a", WORKSPACE), Parcel(2, 5, "a", HALL), crowd=new_crowd(80, seed=4)
    )

    after = at(world, 900.0)

    for parcel_id, kind in (("L5-5", "workspace"), ("L2-5", "game_hall")):
        assert dict(after.admissions.unpaid).get(parcel_id, 0.0) <= (
            FEES[kind] * dict(after.admissions.admitted).get(parcel_id, 0.0) + 1e-6
        )
    assert all(v.purse >= 0 for v in after.crowd.visitors)


def test_a_share_of_the_trips_are_made_by_workers_when_the_dice_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visitors, "WORKER_SHARE", 0.3)
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=new_crowd(200, seed=5))

    after = at(world, 125.0)

    kinds = [v.kind for v in after.crowd.visitors if v.trip]
    assert 0.15 < kinds.count("worker") / len(kinds) < 0.45


def test_a_walk_to_a_workspace_is_the_speed_of_a_walk() -> None:
    world = world_of(Parcel(5, 5, "a", WORKSPACE), crowd=arriving())

    walking = the_visitor(at(world, 5.0))

    assert walking.until == pytest.approx(5.0 + 7.0710678 / VISITOR_SPEED)
