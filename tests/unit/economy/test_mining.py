import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import add_coins, item_count
from aiverse_server.core.world import (
    Inventory,
    MiningJob,
    Pool,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.chips import CHIP_MODELS, ChipModel
from aiverse_server.economy.mining import MINE_SECONDS, finish_mining, next_finish
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import (
    ActionRejected,
    LeaveGame,
    Mine,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.layout import (
    POOL_COUNT,
    POOL_MARGIN,
    POOL_RESERVE,
    POOL_SPACING,
    create_default_world,
    scatter_pools,
)
from aiverse_server.game.lifecycle import end_game

MK1, MK2, MK3 = CHIP_MODELS
MINE_YIELD = MK1.tokens_per_job


def resident(resident_id: str, x: float, y: float, *chips: str) -> Resident:
    """Carries 50 tokens and one chip of each model given, MK1 if none is."""
    held = tuple((model, 1) for model in chips or ("MK1",))
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((TOKEN, 50), *held)),
    )


# a and b stand at the rich pool, c at the poor one, d far from both.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 10, 10),
        resident("b", 13, 10),
        resident("c", 80, 80),
        resident("d", 50, 50),
    ),
    pools=(
        Pool("rich", Position(10, 10), radius=4, reserve=100),
        Pool("poor", Position(80, 80), radius=4, reserve=5),
    ),
    time=7.0,
)


def at(world: World, time: float) -> World:
    return finish_mining(replace(world, time=time))


def test_mining_takes_the_tokens_out_of_the_pool_and_pays_them_when_done() -> None:
    world = perform(WORLD, Mine("a", "rich"))
    early = replace(world, time=7 + MINE_SECONDS - 0.1)
    not_yet = finish_mining(early)
    done = at(world, 7 + MINE_SECONDS)

    assert world.residents[0].mining == MiningJob("rich", 7 + MINE_SECONDS, MINE_YIELD, "MK1")
    assert world.pools[0].reserve == 100 - MINE_YIELD
    assert next_finish(world) == 7 + MINE_SECONDS
    assert not_yet is early
    assert done.residents[0].mining is None
    assert item_count(done, "a", TOKEN) == 50 + MINE_YIELD
    assert next_finish(done) is None


@pytest.mark.parametrize("model", CHIP_MODELS, ids=lambda model: model.model)
def test_a_job_digs_what_its_chip_yields_in_the_same_time(model: ChipModel) -> None:
    world = replace(WORLD, residents=(resident("a", 10, 10, model.model),))

    job = perform(world, Mine("a", "rich")).residents[0].mining

    assert job == MiningJob("rich", 7 + MINE_SECONDS, model.tokens_per_job, model.model)


def test_a_miner_owning_several_chips_uses_the_best() -> None:
    world = replace(WORLD, residents=(resident("a", 10, 10, "MK3", "MK1", "MK2"),))
    job = perform(world, Mine("a", "rich")).residents[0].mining
    assert job is not None
    assert (job.chip, job.amount) == ("MK3", 100)


def test_mining_without_a_chip_is_refused() -> None:
    bare = replace(WORLD.residents[0], inventory=Inventory(((TOKEN, 50),)))
    world = replace(WORLD, residents=(bare, *WORLD.residents[1:]))
    with pytest.raises(ActionRejected) as raised:
        perform(world, Mine("a", "rich"))
    assert raised.value.code == "no_chip"
    assert world.pools[0].reserve == 100


def test_a_chip_set_aside_in_a_sale_offer_cannot_mine() -> None:
    world = perform(WORLD, OfferToSell("a", "b", "MK1", 1, 100))
    with pytest.raises(ActionRejected) as raised:
        perform(world, Mine("a", "rich"))
    assert raised.value.code == "no_chip"


# b offers to buy the chip a mines with.
MINING_WITH_AN_OFFER = perform(
    add_coins(perform(WORLD, Mine("a", "rich")), "b", 100), OfferToBuy("b", "a", "MK1", 1, 100)
)


@pytest.mark.parametrize(
    "action",
    [OfferToSell("a", "b", "MK1", 1, 100), RespondToEvent("a", 1, accept=True)],
    ids=["offering it", "accepting an offer for it"],
)
def test_a_miner_cannot_part_with_the_chip_until_the_job_ends(
    action: OfferToSell | RespondToEvent,
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(MINING_WITH_AN_OFFER, action)

    assert raised.value.code == "busy"


def test_a_chip_changes_hands_once_the_job_is_paid() -> None:
    sold = perform(at(MINING_WITH_AN_OFFER, 100), RespondToEvent("a", 1, accept=True))

    assert item_count(sold, "a", "MK1") == 0
    assert item_count(sold, "b", "MK1") == 2


def test_a_miner_who_sold_their_only_chip_cannot_mine_again() -> None:
    sold = perform(at(MINING_WITH_AN_OFFER, 100), RespondToEvent("a", 1, accept=True))

    with pytest.raises(ActionRejected) as raised:
        perform(sold, Mine("a", "rich"))

    assert raised.value.code == "no_chip"


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 14, 10),))
    assert perform(world, Mine("a", "rich")).residents[0].mining is not None


def test_a_pool_pays_out_at_most_what_it_has_left() -> None:
    mining = perform(WORLD, Mine("c", "poor"))
    paid = at(mining, 100)

    job = mining.residents[2].mining
    assert job is not None
    assert job.amount == 5
    assert item_count(paid, "c", TOKEN) == 55


def test_an_empty_pool_cannot_be_mined() -> None:
    emptied = at(perform(WORLD, Mine("c", "poor")), 100)

    with pytest.raises(ActionRejected) as raised:
        perform(emptied, Mine("c", "poor"))

    assert raised.value.code == "pool_empty"


def test_two_miners_share_a_pool_without_digging_the_same_tokens() -> None:
    mining = perform(perform(WORLD, Mine("a", "rich")), Mine("b", "rich"))
    paid = at(mining, 100)

    assert mining.pools[0].reserve == 100 - 2 * MINE_YIELD
    assert item_count(paid, "a", TOKEN) == item_count(paid, "b", TOKEN) == 50 + MINE_YIELD


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (Mine("d", "rich"), "not_at_pool"),
        (Mine("a", "poor"), "not_at_pool"),
        (Mine("a", "nowhere"), "unknown_pool"),
        (Mine("nobody", "rich"), "unknown_resident"),
    ],
)
def test_mining_is_refused_away_from_a_known_pool(action: Mine, code: str) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, action)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "action",
    [
        Mine("a", "rich"),
        MoveTo("a", Position(10, 11)),
        OfferToBuy("a", "b", "bread", 1, 1),
        WithdrawEvent("a", 1),
        RespondToEvent("a", 1, accept=True),
    ],
)
def test_a_miner_is_busy_until_the_job_ends(
    action: Mine | MoveTo | OfferToBuy | WithdrawEvent | RespondToEvent,
) -> None:
    world = perform(WORLD, Mine("a", "rich"))

    with pytest.raises(ActionRejected) as raised:
        perform(world, action)

    assert raised.value.code == "busy"


def test_a_miner_does_not_hold_others_up() -> None:
    world = perform(WORLD, Mine("a", "rich"))

    moved = perform(world, MoveTo("b", Position(12, 10)))

    assert moved.residents[1].walking is not None


def test_a_miner_may_leave_and_the_tokens_go_back_to_the_pool() -> None:
    world = perform(perform(WORLD, Mine("a", "rich")), LeaveGame("a"))
    assert world.residents[0].left
    assert world.residents[0].mining is None
    assert world.pools[0].reserve == 100


def test_ending_the_game_stops_every_job_unpaid() -> None:
    world = end_game(perform(WORLD, Mine("a", "rich")))
    assert world.residents[0].mining is None
    assert item_count(world, "a", TOKEN) == 50
    assert world.pools[0].reserve == 100


def test_tokens_being_mined_count_as_owned() -> None:
    # a sells every token carried, then mines: the sale completing does not take a out.
    world = perform(add_coins(WORLD, "b", 1), OfferToSell("a", "b", TOKEN, 50, 1))
    world = perform(world, Mine("a", "rich"))

    sold = perform(world, RespondToEvent("b", 1, accept=True))
    paid = at(sold, 100)

    assert not sold.residents[0].left
    assert item_count(paid, "a", TOKEN) == MINE_YIELD


def test_pools_stay_where_they_are() -> None:
    world = at(perform(WORLD, Mine("a", "rich")), 100)
    assert [(p.id, p.position, p.radius) for p in world.pools] == [
        (p.id, p.position, p.radius) for p in WORLD.pools
    ]


def test_the_same_seed_scatters_the_same_pools() -> None:
    assert scatter_pools(42) == scatter_pools(42)
    assert create_default_world(42).pools == scatter_pools(42)
    assert scatter_pools(1) != scatter_pools(2)


@pytest.mark.parametrize("seed", range(20))
def test_pools_are_inside_the_map_apart_and_stocked(seed: int) -> None:
    world = create_default_world(seed)
    assert len(world.pools) == POOL_COUNT
    for pool in world.pools:
        for coordinate in (pool.position.x, pool.position.y):
            assert POOL_MARGIN <= coordinate <= world.map.size - POOL_MARGIN
        assert POOL_RESERVE[0] <= pool.reserve <= POOL_RESERVE[1]
    for i, first in enumerate(world.pools):
        for second in world.pools[i + 1 :]:
            distance = math.dist(
                (first.position.x, first.position.y), (second.position.x, second.position.y)
            )
            assert distance >= POOL_SPACING


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: replace(WORLD, pools=(*WORLD.pools, Pool("rich", Position(1, 1), 1, 1))),
            "pool ids must be unique",
        ),
        (lambda: replace(WORLD, pools=(Pool("out", Position(101, 1), 1, 1),)), "outside the map"),
        (lambda: Pool("negative", Position(1, 1), 1, -1), "holds -1 tokens"),
        (
            lambda: replace(
                WORLD, residents=(replace(WORLD.residents[0], mining=MiningJob("x", 10, 1, "MK1")),)
            ),
            "mines unknown pool",
        ),
        (
            lambda: replace(WORLD.residents[0], mining=MiningJob("rich", 10, 1, "MK2")),
            "without carrying its chip",
        ),
        (lambda: replace(WORLD, time=math.nan), "world time must be a finite"),
    ],
    ids=["same id", "off the map", "negative reserve", "unknown pool", "chip not held", "no time"],
)
def test_the_world_refuses_impossible_pools_and_jobs(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()
