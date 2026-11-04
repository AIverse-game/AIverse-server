"""Digging shards and refining crystals through the agent tools, and what the observation
tells a model about quarries and the workshop."""

from dataclasses import replace

import pytest

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import (
    Inventory,
    Position,
    Quarry,
    Resident,
    Workshop,
    World,
    WorldMap,
)
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.shards import DIG_SECONDS, DIG_YIELD, SHARD
from aiverse_server.economy.workshop import RECIPES
from aiverse_server.server.app import Game

C1_RECIPE = RECIPES[0]

# a stands at quarry q1, b at the workshop with what a C1 takes.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(20, 20), Inventory(((ENERGY, 100),))),
        Resident("b", "乙", "#445566", Position(70, 70), Inventory(((ENERGY, 100), (SHARD, 10)))),
    ),
    quarries=(Quarry("q2", Position(90, 90), 4, 500), Quarry("q1", Position(20, 20), 4, 300)),
    workshop=Workshop(Position(70, 70), 5),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, 600, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_dig_shards_starts_a_job_shown_in_the_status() -> None:
    game, _ = make_game()

    result = ResidentTools(game, "a").dig_shards("q1")

    assert result.ok and result.status is not None
    assert (result.status["digging_quarry"], result.status["digging_done_in"]) == (
        "q1",
        DIG_SECONDS,
    )


def test_shards_dug_show_in_the_next_observation() -> None:
    game, clock = make_game()
    ResidentTools(game, "a").dig_shards("q1")
    clock.now = DIG_SECONDS

    seen = look(game, "a")

    assert (seen.me.shards, seen.me.digging_quarry) == (DIG_YIELD, None)


def test_refine_crystal_spends_the_recipe_and_shows_the_job() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "b")

    result = tools.refine_crystal("C1")
    seen = look(game, "b")

    assert result.ok and result.status is not None
    assert result.status["refining_crystal"] == "C1"
    assert (seen.me.refining_crystal, seen.me.shards) == ("C1", 10 - C1_RECIPE.shards)


def test_refining_away_the_last_energy_in_hand_is_refused() -> None:
    poor = replace(
        WORLD,
        residents=(
            WORLD.residents[0],
            replace(
                WORLD.residents[1], inventory=Inventory(((ENERGY, C1_RECIPE.energy), (SHARD, 10)))
            ),
        ),
    )
    game, _ = make_game(poor)

    result = ResidentTools(game, "b").refine_crystal("C1")

    assert (result.ok, result.code) == (False, "last_energy")
    assert game.world == poor


def test_the_observation_lists_quarries_nearest_first_with_their_reserve() -> None:
    game, _ = make_game()

    seen = look(game, "a")

    assert [(q.id, q.reserve, q.distance) for q in seen.quarries][0] == ("q1", 300, 0)
    assert [q.id for q in seen.quarries] == ["q1", "q2"]


def test_the_observation_names_the_quarry_or_workshop_the_resident_stands_at() -> None:
    game, _ = make_game()

    digger, refiner = look(game, "a"), look(game, "b")

    assert (digger.me.at_quarry, digger.me.at_workshop) == ("q1", False)
    assert (refiner.me.at_quarry, refiner.me.at_workshop) == (None, True)


@pytest.mark.parametrize(("refined", "c1_seconds"), [(0, 15), (2, 12), (8, 7.5)])
def test_the_workshop_recipes_show_the_residents_own_refining_time(
    refined: int, c1_seconds: float
) -> None:
    practised = replace(
        WORLD, residents=(WORLD.residents[0], replace(WORLD.residents[1], crystals_refined=refined))
    )
    game, _ = make_game(practised)

    seen = look(game, "b")

    assert seen.workshop is not None
    recipe = seen.workshop.recipes[0]
    assert (recipe.tier, recipe.shards, recipe.energy, recipe.consumes) == ("C1", 5, 15, None)
    assert recipe.seconds == pytest.approx(c1_seconds)


def test_everyones_crystals_refined_is_public() -> None:
    practised = replace(
        WORLD, residents=(WORLD.residents[0], replace(WORLD.residents[1], crystals_refined=3))
    )
    game, _ = make_game(practised)

    seen = look(game, "a")

    assert [standing.crystals_refined for standing in seen.residents] == [0, 3]
