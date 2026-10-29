import math
from dataclasses import replace

import pytest

from aiverse_server.actions import (
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
from aiverse_server.chips import CHIP_MODELS
from aiverse_server.inventory import add_coins, item_count
from aiverse_server.layout import (
    POOL_COUNT,
    POOL_MARGIN,
    POOL_RESERVE,
    POOL_SPACING,
    create_default_world,
    scatter_pools,
)
from aiverse_server.lifecycle import end_game
from aiverse_server.mining import MINE_SECONDS, finish_mining, next_finish
from aiverse_server.tokens import TOKEN
from aiverse_server.world import Inventory, MiningJob, Pool, Position, Resident, World, WorldMap

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
    assert world.residents[0].mining == MiningJob("rich", 7 + MINE_SECONDS, MINE_YIELD, "MK1")
    assert world.pools[0].reserve == 100 - MINE_YIELD
    assert next_finish(world) == 7 + MINE_SECONDS
    early = replace(world, time=7 + MINE_SECONDS - 0.1)
    assert finish_mining(early) is early
    done = at(world, 7 + MINE_SECONDS)
    assert done.residents[0].mining is None
    assert item_count(done, "a", TOKEN) == 50 + MINE_YIELD
    assert next_finish(done) is None


def test_better_chips_dig_more_in_the_same_time() -> None:
    assert MK1.tokens_per_job < MK2.tokens_per_job < MK3.tokens_per_job
    for model in CHIP_MODELS:
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


def test_a_miner_cannot_part_with_the_chip_until_the_job_ends() -> None:
    world = perform(WORLD, Mine("a", "rich"))
    with pytest.raises(ActionRejected) as raised:
        perform(world, OfferToSell("a", "b", "MK1", 1, 100))
    assert raised.value.code == "busy"
    # Nor can a buyer's offer go through: accepting it is the miner's own primitive.
    world = perform(add_coins(world, "b", 100), OfferToBuy("b", "a", "MK1", 1, 100))
    with pytest.raises(ActionRejected) as raised:
        perform(world, RespondToEvent("a", 1, accept=True))
    assert raised.value.code == "busy"
    # Once paid, the chip is free to go and the next job needs another.
    world = perform(at(world, 100), RespondToEvent("a", 1, accept=True))
    assert item_count(world, "a", "MK1") == 0
    assert item_count(world, "b", "MK1") == 2
    with pytest.raises(ActionRejected) as raised:
        perform(world, Mine("a", "rich"))
    assert raised.value.code == "no_chip"


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 14, 10),))
    assert perform(world, Mine("a", "rich")).residents[0].mining is not None


def test_a_pool_pays_out_at_most_what_it_has_left() -> None:
    world = perform(WORLD, Mine("c", "poor"))
    assert world.residents[2].mining is not None
    assert world.residents[2].mining.amount == 5
    world = at(world, 100)
    assert item_count(world, "c", TOKEN) == 55
    with pytest.raises(ActionRejected) as raised:
        perform(world, Mine("c", "poor"))
    assert raised.value.code == "pool_empty"


def test_two_miners_share_a_pool_without_digging_the_same_tokens() -> None:
    world = perform(perform(WORLD, Mine("a", "rich")), Mine("b", "rich"))
    assert world.pools[0].reserve == 100 - 2 * MINE_YIELD
    world = at(world, 100)
    assert item_count(world, "a", TOKEN) == item_count(world, "b", TOKEN) == 50 + MINE_YIELD


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
    # Others are not held up.
    perform(world, MoveTo("b", Position(12, 10)))


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
    world = perform(world, RespondToEvent("b", 1, accept=True))
    assert not world.residents[0].left
    world = at(world, 100)
    assert item_count(world, "a", TOKEN) == MINE_YIELD


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


def test_the_world_refuses_impossible_pools_and_jobs() -> None:
    with pytest.raises(ValueError):
        replace(WORLD, pools=(*WORLD.pools, Pool("rich", Position(1, 1), 1, 1)))
    with pytest.raises(ValueError):
        replace(WORLD, pools=(Pool("out", Position(101, 1), 1, 1),))
    with pytest.raises(ValueError):
        Pool("negative", Position(1, 1), 1, -1)
    job = MiningJob("nowhere", 10, 1, "MK1")
    with pytest.raises(ValueError):
        replace(WORLD, residents=(replace(WORLD.residents[0], mining=job),))
    # A miner must hold the chip doing the work.
    with pytest.raises(ValueError, match="without carrying its chip"):
        replace(WORLD.residents[0], mining=MiningJob("rich", 10, 1, "MK2"))
    with pytest.raises(ValueError):
        replace(WORLD, time=math.nan)
