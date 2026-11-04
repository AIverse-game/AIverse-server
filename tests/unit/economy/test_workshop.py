import math
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import (
    Inventory,
    Position,
    Resident,
    Workshop,
    World,
    WorldMap,
)
from aiverse_server.economy.crystals import CRYSTAL_TIERS
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.shards import SHARD
from aiverse_server.economy.workshop import RECIPES, next_refine_end, refine_seconds
from aiverse_server.game.actions import (
    ActionRejected,
    LeaveGame,
    MoveTo,
    OfferToSell,
    RefineCrystal,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.layout import (
    MARKET_CLEARANCE,
    PLACE_SPACING,
    create_default_world,
    market_position,
)
from aiverse_server.game.lifecycle import end_game

C1_RECIPE, C2_RECIPE, C3_RECIPE = RECIPES


def resident(resident_id: str, x: float, *stacks: tuple[str, int], energy: int = 200) -> Resident:
    return Resident(
        resident_id, resident_id, "#123456", Position(x, 20), Inventory(((ENERGY, energy), *stacks))
    )


# a and b stand at the workshop, c far from it.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 20, (SHARD, 100), ("C1", 1)),
        resident("b", 22),
        resident("c", 80, (SHARD, 100)),
    ),
    workshop=Workshop(Position(20, 20), 5),
)


def me(world: World, resident_id: str) -> Resident:
    return next(r for r in world.residents if r.id == resident_id)


def test_recipes_cover_every_crystal_tier_each_using_up_the_one_below() -> None:
    assert [recipe.tier for recipe in RECIPES] == [tier.tier for tier in CRYSTAL_TIERS]
    assert [recipe.consumes for recipe in RECIPES] == [None, "C1", "C2"]


def test_refining_spends_the_materials_at_once_and_gives_the_crystal_when_done() -> None:
    started = perform(WORLD, RefineCrystal("a", "C2"))

    done = settle_due(replace(started, time=C2_RECIPE.seconds))

    assert item_count(started, "a", SHARD) == 100 - C2_RECIPE.shards
    assert item_count(started, "a", ENERGY) == 200 - C2_RECIPE.energy
    assert item_count(started, "a", "C1") == 0
    assert item_count(done, "a", "C2") == 1
    assert (me(done, "a").refining, me(done, "a").crystals_refined) == (None, 1)


def test_every_crystal_refined_speeds_up_the_next_down_to_half() -> None:
    assert [refine_seconds(C1_RECIPE, done) for done in (0, 1, 3, 5, 9)] == pytest.approx(
        [15, 13.5, 10.5, 7.5, 7.5]
    )


def test_a_practised_refiner_finishes_sooner() -> None:
    practised = replace(WORLD, residents=(replace(WORLD.residents[0], crystals_refined=2),))

    world = perform(practised, RefineCrystal("a", "C1"))

    refining = me(world, "a").refining
    assert refining is not None and refining.ends_at == pytest.approx(C1_RECIPE.seconds * 0.8)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (RefineCrystal("a", "C9"), "unknown_crystal"),
        (RefineCrystal("c", "C1"), "not_at_workshop"),
        (RefineCrystal("b", "C1"), "not_enough_shards"),
        (RefineCrystal("a", "C3"), "no_lower_crystal"),
    ],
    ids=["unknown_tier", "too_far", "no_shards", "no_c2"],
)
def test_a_refine_that_cannot_happen_is_refused(action: RefineCrystal, code: str) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, action)

    assert rejected.value.code == code


def test_refining_needs_the_energy_carried() -> None:
    poor = replace(WORLD, residents=(resident("a", 20, (SHARD, 100), energy=10),))

    with pytest.raises(ActionRejected) as rejected:
        perform(poor, RefineCrystal("a", "C1"))

    assert rejected.value.code == "not_enough_energy"


def test_shards_set_aside_in_an_offer_cannot_be_refined() -> None:
    offered = perform(WORLD, OfferToSell("a", "b", SHARD, 98, 10))

    with pytest.raises(ActionRejected) as rejected:
        perform(offered, RefineCrystal("a", "C1"))

    assert rejected.value.code == "not_enough_shards"


def test_a_refiner_is_busy_until_the_crystal_is_done() -> None:
    world = perform(WORLD, RefineCrystal("a", "C1"))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, MoveTo("a", Position(30, 30)))

    assert rejected.value.code == "busy"


def test_the_game_ending_drops_a_refine_without_handing_back_its_materials() -> None:
    started = perform(WORLD, RefineCrystal("a", "C1"))

    ended = end_game(started)

    a = me(ended, "a")
    assert (a.refining, a.crystals_refined) == (None, 0)
    assert item_count(ended, "a", SHARD) == 100 - C1_RECIPE.shards
    assert item_count(ended, "a", "C1") == 1


def test_leaving_drops_a_refine() -> None:
    started = perform(WORLD, RefineCrystal("a", "C1"))

    left = perform(started, LeaveGame("a"))

    assert (me(left, "a").left, me(left, "a").refining) == (True, None)


def test_refining_away_the_last_energy_owned_takes_the_refiner_out() -> None:
    exact = replace(WORLD, residents=(resident("a", 20, (SHARD, 100), energy=C1_RECIPE.energy),))

    world = perform(exact, RefineCrystal("a", "C1"))

    assert (me(world, "a").left, me(world, "a").refining) == (True, None)


def test_the_clock_wakes_for_the_earliest_refine() -> None:
    world = perform(WORLD, RefineCrystal("a", "C1"))

    assert next_refine_end(WORLD) is None
    assert next_refine_end(world) == C1_RECIPE.seconds


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_the_default_world_places_the_workshop_apart_from_everything_else(seed: int) -> None:
    world = create_default_world(seed)

    assert world.workshop is not None
    place = world.workshop.position
    market = market_position()
    others = [spring.position for spring in world.springs] + [q.position for q in world.quarries]
    assert math.dist((place.x, place.y), (market.x, market.y)) >= MARKET_CLEARANCE
    assert all(math.dist((place.x, place.y), (o.x, o.y)) >= PLACE_SPACING for o in others)
