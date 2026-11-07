import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import (
    GatherJob,
    Inventory,
    Mine,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.gathering import DATA, GATHER_SECONDS, GATHER_YIELD, next_gather_end
from aiverse_server.game.actions import ActionRejected, GatherData, LeaveGame, MoveTo, perform
from aiverse_server.game.clock import settle_due
from aiverse_server.game.layout import (
    MARKET_CLEARANCE,
    MINE_COUNT,
    PLACE_SPACING,
    create_default_world,
    market_position,
    mine_reserve,
    scatter_springs,
)
from aiverse_server.game.lifecycle import end_game


def resident(resident_id: str, x: float, y: float) -> Resident:
    return Resident(
        resident_id, resident_id, "#123456", Position(x, y), Inventory(((COMPUTE, 50),))
    )


# a and b stand at the rich mine, c at the nearly empty one, d far from both.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 20, 20),
        resident("b", 22, 20),
        resident("c", 80, 80),
        resident("d", 50, 50),
    ),
    mines=(Mine("rich", Position(20, 20), 4, 300), Mine("poor", Position(80, 80), 4, 4)),
)


def reserve(world: World, mine_id: str) -> int:
    return next(mine.reserve for mine in world.mines if mine.id == mine_id)


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def test_gathering_takes_the_data_out_of_the_mine_and_pays_them_when_done() -> None:
    started = perform(WORLD, GatherData("a", "rich"))

    done = at(started, GATHER_SECONDS)

    assert reserve(started, "rich") == 300 - GATHER_YIELD
    assert item_count(started, "a", DATA) == 0
    assert item_count(done, "a", DATA) == GATHER_YIELD
    assert next(r for r in done.residents if r.id == "a").gathering is None


def test_gathering_needs_no_program() -> None:
    world = perform(WORLD, GatherData("a", "rich"))

    assert next(r for r in world.residents if r.id == "a").gathering == GatherJob(
        "rich", GATHER_SECONDS, GATHER_YIELD
    )


def test_the_last_gather_takes_only_what_the_mine_has_left() -> None:
    world = at(perform(WORLD, GatherData("c", "poor")), GATHER_SECONDS)

    assert (reserve(world, "poor"), item_count(world, "c", DATA)) == (0, 4)


def test_two_gatherers_share_a_mine_without_gathering_the_same_data() -> None:
    world = perform(perform(WORLD, GatherData("a", "rich")), GatherData("b", "rich"))

    assert reserve(world, "rich") == 300 - 2 * GATHER_YIELD


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, GatherData("a", "nowhere"), "unknown_mine"),
        (WORLD, GatherData("d", "rich"), "not_at_mine"),
        (
            replace(WORLD, mines=(Mine("rich", Position(20, 20), 4, 0),)),
            GatherData("a", "rich"),
            "mine_empty",
        ),
        (perform(WORLD, GatherData("a", "rich")), GatherData("a", "rich"), "busy"),
        (perform(WORLD, GatherData("a", "rich")), MoveTo("a", Position(30, 30)), "busy"),
    ],
    ids=["unknown", "too_far", "empty", "already_gathering", "walk_while_gathering"],
)
def test_a_gather_that_cannot_happen_is_refused(
    world: World, action: GatherData | MoveTo, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(world, action)

    assert rejected.value.code == code


@pytest.mark.parametrize(
    "stop",
    [lambda world: perform(world, LeaveGame("a")), end_game],
    ids=["leaving", "game_end"],
)
def test_stopping_a_gather_puts_the_data_back(stop: Callable[[World], World]) -> None:
    started = perform(WORLD, GatherData("a", "rich"))

    stopped = stop(started)

    assert reserve(stopped, "rich") == 300
    assert next(r for r in stopped.residents if r.id == "a").gathering is None


def test_the_clock_wakes_for_the_earliest_gather() -> None:
    world = perform(WORLD, GatherData("a", "rich"))

    assert next_gather_end(WORLD) is None
    assert next_gather_end(world) == GATHER_SECONDS


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_the_default_world_places_mines_apart_from_the_market_and_springs(seed: int) -> None:
    world = create_default_world(seed)

    market = market_position()
    springs = [spring.position for spring in world.springs]
    mines = [mine.position for mine in world.mines]
    assert [mine.id for mine in world.mines] == [f"m{n}" for n in range(1, MINE_COUNT + 1)]
    low, high = mine_reserve(len(world.residents))
    assert all(low <= mine.reserve <= high for mine in world.mines)
    for i, place in enumerate(mines):
        assert math.dist((place.x, place.y), (market.x, market.y)) >= MARKET_CLEARANCE
        for other in springs + mines[:i]:
            assert math.dist((place.x, place.y), (other.x, other.y)) >= PLACE_SPACING


def test_mine_reserves_grow_with_the_number_of_residents() -> None:
    assert [mine_reserve(n) for n in (4, 8)] == [(300, 600), (600, 1_200)]


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_placing_mines_leaves_a_seeds_springs_where_they_were(seed: int) -> None:
    assert create_default_world(seed).springs == scatter_springs(seed)
