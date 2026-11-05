import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import add_coins, item_count
from aiverse_server.core.world import (
    HarvestJob,
    Inventory,
    Position,
    Resident,
    Spring,
    World,
    WorldMap,
)
from aiverse_server.economy.crystals import CRYSTAL_TIERS, CrystalTier
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.harvesting import HARVEST_SECONDS, finish_harvesting, next_finish
from aiverse_server.game.actions import (
    ActionRejected,
    Harvest,
    LeaveGame,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.layout import (
    SPRING_COUNT,
    SPRING_MARGIN,
    SPRING_SPACING,
    create_default_world,
    scatter_springs,
    spring_reserve,
)
from aiverse_server.game.lifecycle import end_game

C1, C2, C3 = CRYSTAL_TIERS
HARVEST_YIELD = C1.energy_per_job


def resident(resident_id: str, x: float, y: float, *crystals: str) -> Resident:
    """Carries 50 energy and one crystal of each tier given, C1 if none is."""
    held = tuple((tier, 1) for tier in crystals or ("C1",))
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((ENERGY, 50), *held)),
    )


# a and b stand at the rich spring, c at the poor one, d far from both.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 10, 10),
        resident("b", 13, 10),
        resident("c", 80, 80),
        resident("d", 50, 50),
    ),
    springs=(
        Spring("rich", Position(10, 10), radius=4, reserve=100),
        Spring("poor", Position(80, 80), radius=4, reserve=5),
    ),
    time=7.0,
)


def at(world: World, time: float) -> World:
    return finish_harvesting(replace(world, time=time))


def test_harvesting_takes_the_energy_out_of_the_spring_and_pays_them_when_done() -> None:
    world = perform(WORLD, Harvest("a", "rich"))
    early = replace(world, time=7 + HARVEST_SECONDS - 0.1)
    not_yet = finish_harvesting(early)
    done = at(world, 7 + HARVEST_SECONDS)

    assert world.residents[0].harvesting == HarvestJob(
        "rich", 7 + HARVEST_SECONDS, HARVEST_YIELD, "C1"
    )
    assert world.springs[0].reserve == 100 - HARVEST_YIELD
    assert next_finish(world) == 7 + HARVEST_SECONDS
    assert not_yet is early
    assert done.residents[0].harvesting is None
    assert item_count(done, "a", ENERGY) == 50 + HARVEST_YIELD
    assert next_finish(done) is None


@pytest.mark.parametrize("tier", CRYSTAL_TIERS, ids=lambda tier: tier.tier)
def test_a_job_digs_what_its_crystal_yields_in_the_same_time(tier: CrystalTier) -> None:
    world = replace(WORLD, residents=(resident("a", 10, 10, tier.tier),))

    job = perform(world, Harvest("a", "rich")).residents[0].harvesting

    assert job == HarvestJob("rich", 7 + HARVEST_SECONDS, tier.energy_per_job, tier.tier)


def test_a_harvester_owning_several_crystals_uses_the_best() -> None:
    world = replace(WORLD, residents=(resident("a", 10, 10, "C3", "C1", "C2"),))
    job = perform(world, Harvest("a", "rich")).residents[0].harvesting
    assert job is not None
    assert (job.crystal, job.amount) == ("C3", 100)


def test_harvesting_without_a_crystal_is_refused() -> None:
    bare = replace(WORLD.residents[0], inventory=Inventory(((ENERGY, 50),)))
    world = replace(WORLD, residents=(bare, *WORLD.residents[1:]))
    with pytest.raises(ActionRejected) as raised:
        perform(world, Harvest("a", "rich"))
    assert raised.value.code == "no_crystal"
    assert world.springs[0].reserve == 100


def test_a_crystal_set_aside_in_a_sale_offer_cannot_harvest() -> None:
    world = perform(WORLD, OfferToSell("a", "b", "C1", 1, 100))
    with pytest.raises(ActionRejected) as raised:
        perform(world, Harvest("a", "rich"))
    assert raised.value.code == "no_crystal"


# b offers to buy the crystal a harvests with.
HARVESTING_WITH_AN_OFFER = perform(
    add_coins(perform(WORLD, Harvest("a", "rich")), "b", 100), OfferToBuy("b", "a", "C1", 1, 100)
)


@pytest.mark.parametrize(
    "action",
    [OfferToSell("a", "b", "C1", 1, 100), RespondToEvent("a", 1, accept=True)],
    ids=["offering it", "accepting an offer for it"],
)
def test_a_harvester_cannot_part_with_the_crystal_until_the_job_ends(
    action: OfferToSell | RespondToEvent,
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(HARVESTING_WITH_AN_OFFER, action)

    assert raised.value.code == "busy"


def test_a_crystal_changes_hands_once_the_job_is_paid() -> None:
    sold = perform(at(HARVESTING_WITH_AN_OFFER, 100), RespondToEvent("a", 1, accept=True))

    assert item_count(sold, "a", "C1") == 0
    assert item_count(sold, "b", "C1") == 2


def test_a_harvester_who_sold_their_only_crystal_cannot_harvest_again() -> None:
    sold = perform(at(HARVESTING_WITH_AN_OFFER, 100), RespondToEvent("a", 1, accept=True))

    with pytest.raises(ActionRejected) as raised:
        perform(sold, Harvest("a", "rich"))

    assert raised.value.code == "no_crystal"


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 14, 10),))
    assert perform(world, Harvest("a", "rich")).residents[0].harvesting is not None


def test_a_spring_pays_out_at_most_what_it_has_left() -> None:
    harvesting = perform(WORLD, Harvest("c", "poor"))
    paid = at(harvesting, 100)

    job = harvesting.residents[2].harvesting
    assert job is not None
    assert job.amount == 5
    assert item_count(paid, "c", ENERGY) == 55


def test_an_empty_spring_cannot_be_harvested() -> None:
    emptied = at(perform(WORLD, Harvest("c", "poor")), 100)

    with pytest.raises(ActionRejected) as raised:
        perform(emptied, Harvest("c", "poor"))

    assert raised.value.code == "spring_empty"


def test_two_harvesters_share_a_spring_without_digging_the_same_energy() -> None:
    harvesting = perform(perform(WORLD, Harvest("a", "rich")), Harvest("b", "rich"))
    paid = at(harvesting, 100)

    assert harvesting.springs[0].reserve == 100 - 2 * HARVEST_YIELD
    assert item_count(paid, "a", ENERGY) == item_count(paid, "b", ENERGY) == 50 + HARVEST_YIELD


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (Harvest("d", "rich"), "not_at_spring"),
        (Harvest("a", "poor"), "not_at_spring"),
        (Harvest("a", "nowhere"), "unknown_spring"),
        (Harvest("nobody", "rich"), "unknown_resident"),
    ],
)
def test_harvesting_is_refused_away_from_a_known_spring(action: Harvest, code: str) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, action)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "action",
    [
        Harvest("a", "rich"),
        MoveTo("a", Position(10, 11)),
        OfferToBuy("a", "b", "bread", 1, 1),
        WithdrawEvent("a", 1),
        RespondToEvent("a", 1, accept=True),
    ],
)
def test_a_harvester_is_busy_until_the_job_ends(
    action: Harvest | MoveTo | OfferToBuy | WithdrawEvent | RespondToEvent,
) -> None:
    world = perform(WORLD, Harvest("a", "rich"))

    with pytest.raises(ActionRejected) as raised:
        perform(world, action)

    assert raised.value.code == "busy"


def test_a_harvester_does_not_hold_others_up() -> None:
    world = perform(WORLD, Harvest("a", "rich"))

    moved = perform(world, MoveTo("b", Position(12, 10)))

    assert moved.residents[1].walking is not None


def test_a_harvester_may_leave_and_the_energy_goes_back_to_the_spring() -> None:
    world = perform(perform(WORLD, Harvest("a", "rich")), LeaveGame("a"))
    assert world.residents[0].left
    assert world.residents[0].harvesting is None
    assert world.springs[0].reserve == 100


def test_ending_the_game_stops_every_job_unpaid() -> None:
    world = end_game(perform(WORLD, Harvest("a", "rich")))
    assert world.residents[0].harvesting is None
    assert item_count(world, "a", ENERGY) == 50
    assert world.springs[0].reserve == 100


def test_energy_being_harvested_count_as_owned() -> None:
    # a sells every energy carried, then harvests: the sale completing does not take a out.
    world = perform(add_coins(WORLD, "b", 1), OfferToSell("a", "b", ENERGY, 50, 1))
    world = perform(world, Harvest("a", "rich"))

    sold = perform(world, RespondToEvent("b", 1, accept=True))
    paid = at(sold, 100)

    assert not sold.residents[0].left
    assert item_count(paid, "a", ENERGY) == HARVEST_YIELD


def test_springs_stay_where_they_are() -> None:
    world = at(perform(WORLD, Harvest("a", "rich")), 100)
    assert [(p.id, p.position, p.radius) for p in world.springs] == [
        (p.id, p.position, p.radius) for p in WORLD.springs
    ]


@pytest.mark.parametrize("residents", [1, 4, 8])
def test_spring_reserves_grow_with_the_number_of_residents(residents: int) -> None:
    springs = scatter_springs(42, residents=residents)

    assert all(375 * residents <= s.reserve <= 1_000 * residents for s in springs)


def test_four_residents_keep_the_spring_reserves_of_the_old_fixed_range() -> None:
    assert spring_reserve(4) == (1_500, 4_000)


def test_a_seed_with_four_residents_scatters_the_springs_it_did_before() -> None:
    springs = scatter_springs(42, residents=4)

    # What seed 42 gave when every game had four residents and a fixed reserve range.
    assert [(s.id, s.position.x, s.position.y, s.reserve) for s in springs] == [
        ("s1", 91.7, 18.0, 2626),
        ("s2", 44.4, 31.7, 1919),
        ("s3", 96.2, 122.1, 1856),
        ("s4", 48.4, 119.3, 2153),
        ("s5", 98.8, 55.8, 2136),
        ("s6", 27.3, 60.6, 2970),
    ]


def test_the_same_seed_scatters_the_same_springs() -> None:
    assert scatter_springs(42) == scatter_springs(42)
    assert create_default_world(42).springs == scatter_springs(42)
    assert scatter_springs(1) != scatter_springs(2)


@pytest.mark.parametrize("seed", range(20))
def test_springs_are_inside_the_map_apart_and_stocked(seed: int) -> None:
    world = create_default_world(seed)
    assert len(world.springs) == SPRING_COUNT
    for spring in world.springs:
        for coordinate in (spring.position.x, spring.position.y):
            assert SPRING_MARGIN <= coordinate <= world.map.size - SPRING_MARGIN
        low, high = spring_reserve(len(world.residents))
        assert low <= spring.reserve <= high
    for i, first in enumerate(world.springs):
        for second in world.springs[i + 1 :]:
            distance = math.dist(
                (first.position.x, first.position.y), (second.position.x, second.position.y)
            )
            assert distance >= SPRING_SPACING


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: replace(WORLD, springs=(*WORLD.springs, Spring("rich", Position(1, 1), 1, 1))),
            "spring ids must be unique",
        ),
        (
            lambda: replace(WORLD, springs=(Spring("out", Position(101, 1), 1, 1),)),
            "outside the map",
        ),
        (lambda: Spring("negative", Position(1, 1), 1, -1), "holds -1 energy"),
        (
            lambda: replace(
                WORLD,
                residents=(replace(WORLD.residents[0], harvesting=HarvestJob("x", 10, 1, "C1")),),
            ),
            "harvests unknown spring",
        ),
        (
            lambda: replace(WORLD.residents[0], harvesting=HarvestJob("rich", 10, 1, "C2")),
            "without carrying its crystal",
        ),
        (lambda: replace(WORLD, time=math.nan), "world time must be a finite"),
    ],
    ids=[
        "same id",
        "off the map",
        "negative reserve",
        "unknown spring",
        "crystal not held",
        "no time",
    ],
)
def test_the_world_refuses_impossible_springs_and_jobs(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()
