"""Gathering data and writing programs through the agent tools, and what the observation
tells a model about mines and the studio."""

from dataclasses import replace

import pytest

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import (
    Inventory,
    Mine,
    Position,
    Resident,
    Studio,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.gathering import DATA, GATHER_SECONDS, GATHER_YIELD
from aiverse_server.economy.studio import RECIPES
from aiverse_server.server.app import Game

V1_RECIPE = RECIPES[0]

# a stands at mine m1, b at the studio with what a v1 takes.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(20, 20), Inventory(((COMPUTE, 100),))),
        Resident("b", "乙", "#445566", Position(70, 70), Inventory(((COMPUTE, 100), (DATA, 10)))),
    ),
    mines=(Mine("m2", Position(90, 90), 4, 500), Mine("m1", Position(20, 20), 4, 300)),
    studio=Studio(Position(70, 70), 5),
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


def test_gather_data_starts_a_job_shown_in_the_status() -> None:
    game, _ = make_game()

    result = ResidentTools(game, "a").gather_data("m1")

    assert result.ok and result.status is not None
    assert (result.status["gathering_mine"], result.status["gathering_done_in"]) == (
        "m1",
        GATHER_SECONDS,
    )


def test_data_gathered_shows_in_the_next_observation() -> None:
    game, clock = make_game()
    ResidentTools(game, "a").gather_data("m1")
    clock.now = GATHER_SECONDS

    seen = look(game, "a")

    assert (seen.me.data, seen.me.gathering_mine) == (GATHER_YIELD, None)


def test_write_program_spends_the_recipe_and_shows_the_job() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "b")

    result = tools.write_program("v1")
    seen = look(game, "b")

    assert result.ok and result.status is not None
    assert result.status["writing_program"] == "v1"
    assert (seen.me.writing_program, seen.me.data) == ("v1", 10 - V1_RECIPE.data)


def test_writing_away_the_last_compute_in_hand_is_refused() -> None:
    poor = replace(
        WORLD,
        residents=(
            WORLD.residents[0],
            replace(
                WORLD.residents[1], inventory=Inventory(((COMPUTE, V1_RECIPE.compute), (DATA, 10)))
            ),
        ),
    )
    game, _ = make_game(poor)

    result = ResidentTools(game, "b").write_program("v1")

    assert (result.ok, result.code) == (False, "last_compute")
    assert game.world == poor


def test_the_observation_lists_mines_nearest_first_with_their_reserve() -> None:
    game, _ = make_game()

    seen = look(game, "a")

    assert [(q.id, q.reserve, q.distance) for q in seen.mines][0] == ("m1", 300, 0)
    assert [q.id for q in seen.mines] == ["m1", "m2"]


def test_the_observation_names_the_mine_or_studio_the_resident_stands_at() -> None:
    game, _ = make_game()

    gatherer, writer = look(game, "a"), look(game, "b")

    assert (gatherer.me.at_mine, gatherer.me.at_studio) == ("m1", False)
    assert (writer.me.at_mine, writer.me.at_studio) == (None, True)


@pytest.mark.parametrize(("written", "v1_seconds"), [(0, 15), (2, 12), (8, 7.5)])
def test_the_studio_recipes_show_the_residents_own_writing_time(
    written: int, v1_seconds: float
) -> None:
    practised = replace(
        WORLD, residents=(WORLD.residents[0], replace(WORLD.residents[1], programs_written=written))
    )
    game, _ = make_game(practised)

    seen = look(game, "b")

    assert seen.studio is not None
    recipe = seen.studio.recipes[0]
    assert (recipe.tier, recipe.data, recipe.compute, recipe.consumes) == ("v1", 5, 15, None)
    assert recipe.seconds == pytest.approx(v1_seconds)


def test_everyones_programs_written_is_public() -> None:
    practised = replace(
        WORLD, residents=(WORLD.residents[0], replace(WORLD.residents[1], programs_written=3))
    )
    game, _ = make_game(practised)

    seen = look(game, "a")

    assert [standing.programs_written for standing in seen.residents] == [0, 3]
