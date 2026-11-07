import math
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import (
    Inventory,
    Position,
    Resident,
    Studio,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.gathering import DATA
from aiverse_server.economy.programs import PROGRAM_TIERS
from aiverse_server.economy.studio import RECIPES, next_write_end, write_seconds
from aiverse_server.game.actions import (
    ActionRejected,
    LeaveGame,
    MoveTo,
    OfferToSell,
    WriteProgram,
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

V1_RECIPE, V2_RECIPE, V3_RECIPE = RECIPES


def resident(resident_id: str, x: float, *stacks: tuple[str, int], compute: int = 200) -> Resident:
    return Resident(
        resident_id,
        resident_id,
        "#123456",
        Position(x, 20),
        Inventory(((COMPUTE, compute), *stacks)),
    )


# a and b stand at the studio, c far from it.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 20, (DATA, 100), ("v1", 1)),
        resident("b", 22),
        resident("c", 80, (DATA, 100)),
    ),
    studio=Studio(Position(20, 20), 5),
)


def me(world: World, resident_id: str) -> Resident:
    return next(r for r in world.residents if r.id == resident_id)


def test_recipes_cover_every_program_tier_each_using_up_the_one_below() -> None:
    assert [recipe.tier for recipe in RECIPES] == [tier.tier for tier in PROGRAM_TIERS]
    assert [recipe.consumes for recipe in RECIPES] == [None, "v1", "v2"]


def test_writing_spends_the_materials_at_once_and_gives_the_program_when_done() -> None:
    started = perform(WORLD, WriteProgram("a", "v2"))

    done = settle_due(replace(started, time=V2_RECIPE.seconds))

    assert item_count(started, "a", DATA) == 100 - V2_RECIPE.data
    assert item_count(started, "a", COMPUTE) == 200 - V2_RECIPE.compute
    assert item_count(started, "a", "v1") == 0
    assert item_count(done, "a", "v2") == 1
    assert (me(done, "a").writing, me(done, "a").programs_written) == (None, 1)


def test_every_program_written_speeds_up_the_next_down_to_half() -> None:
    assert [write_seconds(V1_RECIPE, done) for done in (0, 1, 3, 5, 9)] == pytest.approx(
        [15, 13.5, 10.5, 7.5, 7.5]
    )


def test_a_practised_writer_finishes_sooner() -> None:
    practised = replace(WORLD, residents=(replace(WORLD.residents[0], programs_written=2),))

    world = perform(practised, WriteProgram("a", "v1"))

    writing = me(world, "a").writing
    assert writing is not None and writing.ends_at == pytest.approx(V1_RECIPE.seconds * 0.8)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (WriteProgram("a", "C9"), "unknown_program"),
        (WriteProgram("c", "v1"), "not_at_studio"),
        (WriteProgram("b", "v1"), "not_enough_data"),
        (WriteProgram("a", "v3"), "no_lower_program"),
    ],
    ids=["unknown_tier", "too_far", "no_data", "no_c2"],
)
def test_a_write_that_cannot_happen_is_refused(action: WriteProgram, code: str) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, action)

    assert rejected.value.code == code


def test_writing_needs_the_compute_carried() -> None:
    poor = replace(WORLD, residents=(resident("a", 20, (DATA, 100), compute=10),))

    with pytest.raises(ActionRejected) as rejected:
        perform(poor, WriteProgram("a", "v1"))

    assert rejected.value.code == "not_enough_compute"


def test_data_set_aside_in_an_offer_cannot_be_written() -> None:
    offered = perform(WORLD, OfferToSell("a", "b", DATA, 98, 10))

    with pytest.raises(ActionRejected) as rejected:
        perform(offered, WriteProgram("a", "v1"))

    assert rejected.value.code == "not_enough_data"


def test_a_writer_is_busy_until_the_program_is_done() -> None:
    world = perform(WORLD, WriteProgram("a", "v1"))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, MoveTo("a", Position(30, 30)))

    assert rejected.value.code == "busy"


def test_the_game_ending_drops_a_write_without_handing_back_its_materials() -> None:
    started = perform(WORLD, WriteProgram("a", "v1"))

    ended = end_game(started)

    a = me(ended, "a")
    assert (a.writing, a.programs_written) == (None, 0)
    assert item_count(ended, "a", DATA) == 100 - V1_RECIPE.data
    assert item_count(ended, "a", "v1") == 1


def test_leaving_drops_a_write() -> None:
    started = perform(WORLD, WriteProgram("a", "v1"))

    left = perform(started, LeaveGame("a"))

    assert (me(left, "a").left, me(left, "a").writing) == (True, None)


def test_writing_away_the_last_compute_owned_takes_the_writer_out() -> None:
    exact = replace(WORLD, residents=(resident("a", 20, (DATA, 100), compute=V1_RECIPE.compute),))

    world = perform(exact, WriteProgram("a", "v1"))

    assert (me(world, "a").left, me(world, "a").writing) == (True, None)


def test_the_clock_wakes_for_the_earliest_write() -> None:
    world = perform(WORLD, WriteProgram("a", "v1"))

    assert next_write_end(WORLD) is None
    assert next_write_end(world) == V1_RECIPE.seconds


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_the_default_world_places_the_studio_apart_from_everything_else(seed: int) -> None:
    world = create_default_world(seed)

    assert world.studio is not None
    place = world.studio.position
    market = market_position()
    others = [spring.position for spring in world.springs] + [q.position for q in world.mines]
    assert math.dist((place.x, place.y), (market.x, market.y)) >= MARKET_CLEARANCE
    assert all(math.dist((place.x, place.y), (o.x, o.y)) >= PLACE_SPACING for o in others)
