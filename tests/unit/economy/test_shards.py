import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import (
    DigJob,
    Inventory,
    Position,
    Quarry,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.shards import DIG_SECONDS, DIG_YIELD, SHARD, next_dig_end
from aiverse_server.game.actions import ActionRejected, DigShards, LeaveGame, MoveTo, perform
from aiverse_server.game.clock import settle_due
from aiverse_server.game.layout import (
    MARKET_CLEARANCE,
    PLACE_SPACING,
    QUARRY_COUNT,
    create_default_world,
    market_position,
    quarry_reserve,
    scatter_springs,
)
from aiverse_server.game.lifecycle import end_game


def resident(resident_id: str, x: float, y: float) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(x, y), Inventory(((ENERGY, 50),)))


# a and b stand at the rich quarry, c at the nearly empty one, d far from both.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 20, 20),
        resident("b", 22, 20),
        resident("c", 80, 80),
        resident("d", 50, 50),
    ),
    quarries=(Quarry("rich", Position(20, 20), 4, 300), Quarry("poor", Position(80, 80), 4, 4)),
)


def reserve(world: World, quarry_id: str) -> int:
    return next(quarry.reserve for quarry in world.quarries if quarry.id == quarry_id)


def at(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def test_digging_takes_the_shards_out_of_the_quarry_and_pays_them_when_done() -> None:
    started = perform(WORLD, DigShards("a", "rich"))

    done = at(started, DIG_SECONDS)

    assert reserve(started, "rich") == 300 - DIG_YIELD
    assert item_count(started, "a", SHARD) == 0
    assert item_count(done, "a", SHARD) == DIG_YIELD
    assert next(r for r in done.residents if r.id == "a").digging is None


def test_digging_needs_no_crystal() -> None:
    world = perform(WORLD, DigShards("a", "rich"))

    assert next(r for r in world.residents if r.id == "a").digging == DigJob(
        "rich", DIG_SECONDS, DIG_YIELD
    )


def test_the_last_dig_takes_only_what_the_quarry_has_left() -> None:
    world = at(perform(WORLD, DigShards("c", "poor")), DIG_SECONDS)

    assert (reserve(world, "poor"), item_count(world, "c", SHARD)) == (0, 4)


def test_two_diggers_share_a_quarry_without_digging_the_same_shards() -> None:
    world = perform(perform(WORLD, DigShards("a", "rich")), DigShards("b", "rich"))

    assert reserve(world, "rich") == 300 - 2 * DIG_YIELD


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, DigShards("a", "nowhere"), "unknown_quarry"),
        (WORLD, DigShards("d", "rich"), "not_at_quarry"),
        (
            replace(WORLD, quarries=(Quarry("rich", Position(20, 20), 4, 0),)),
            DigShards("a", "rich"),
            "quarry_empty",
        ),
        (perform(WORLD, DigShards("a", "rich")), DigShards("a", "rich"), "busy"),
        (perform(WORLD, DigShards("a", "rich")), MoveTo("a", Position(30, 30)), "busy"),
    ],
    ids=["unknown", "too_far", "empty", "already_digging", "walk_while_digging"],
)
def test_a_dig_that_cannot_happen_is_refused(
    world: World, action: DigShards | MoveTo, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(world, action)

    assert rejected.value.code == code


@pytest.mark.parametrize(
    "stop",
    [lambda world: perform(world, LeaveGame("a")), end_game],
    ids=["leaving", "game_end"],
)
def test_stopping_a_dig_puts_the_shards_back(stop: Callable[[World], World]) -> None:
    started = perform(WORLD, DigShards("a", "rich"))

    stopped = stop(started)

    assert reserve(stopped, "rich") == 300
    assert next(r for r in stopped.residents if r.id == "a").digging is None


def test_the_clock_wakes_for_the_earliest_dig() -> None:
    world = perform(WORLD, DigShards("a", "rich"))

    assert next_dig_end(WORLD) is None
    assert next_dig_end(world) == DIG_SECONDS


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_the_default_world_places_quarries_apart_from_the_market_and_springs(seed: int) -> None:
    world = create_default_world(seed)

    market = market_position()
    springs = [spring.position for spring in world.springs]
    quarries = [quarry.position for quarry in world.quarries]
    assert len(world.quarries) == QUARRY_COUNT
    low, high = quarry_reserve(len(world.residents))
    assert all(low <= q.reserve <= high for q in world.quarries)
    for i, place in enumerate(quarries):
        assert math.dist((place.x, place.y), (market.x, market.y)) >= MARKET_CLEARANCE
        for other in springs + quarries[:i]:
            assert math.dist((place.x, place.y), (other.x, other.y)) >= PLACE_SPACING


def test_quarry_reserves_grow_with_the_number_of_residents() -> None:
    assert [quarry_reserve(n) for n in (4, 8)] == [(300, 600), (600, 1_200)]


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_placing_quarries_leaves_a_seeds_springs_where_they_were(seed: int) -> None:
    assert create_default_world(seed).springs == scatter_springs(seed)
