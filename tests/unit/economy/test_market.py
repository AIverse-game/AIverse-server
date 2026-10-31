import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.world import (
    Inventory,
    Market,
    MiningJob,
    Pool,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.market import (
    MARKET_BASE_PRICE,
    MARKET_DEPTH,
    MARKET_HALF_LIFE,
    MARKET_RADIUS,
    pressure_at,
    quote,
    unit_price,
)
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import (
    ActionRejected,
    LeaveGame,
    OfferToSell,
    SellTokens,
    perform,
)
from aiverse_server.game.layout import MARKET_CLEARANCE, create_default_world, market_position
from aiverse_server.game.lifecycle import end_game
from aiverse_server.residents.perception import begin_turn


def resident(resident_id: str, x: float, y: float, tokens: int = 500) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((TOKEN, tokens),)),
        coins=100,
    )


# a and b stand at the market, c far from it next to a pool.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 50, 50), resident("b", 53, 54), resident("c", 10, 10)),
    pools=(Pool("p1", Position(10, 10), radius=4, reserve=100),),
    market=Market(Position(50, 50), radius=5),
    time=7.0,
)


def coins(world: World, resident_id: str) -> int:
    return next(r.coins for r in world.residents if r.id == resident_id)


def test_selling_pays_the_price_summed_over_the_sale() -> None:
    world = perform(WORLD, SellTokens("a", 30))
    expected = math.floor(MARKET_BASE_PRICE * MARKET_DEPTH * math.log(130 / 100))
    assert expected == 524
    assert coins(world, "a") == 100 + expected
    assert item_count(world, "a", TOKEN) == 470
    assert world.market == Market(Position(50, 50), 5, pressure=30, pressure_at=7.0)


def test_the_price_falls_with_sales_and_recovers_with_time() -> None:
    assert unit_price(WORLD.market, 7.0) == MARKET_BASE_PRICE  # type: ignore[arg-type]

    world = perform(WORLD, SellTokens("a", 100))

    market = world.market
    assert market is not None
    assert unit_price(market, 7.0) == pytest.approx(MARKET_BASE_PRICE / 2)
    assert pressure_at(market, 7.0 + MARKET_HALF_LIFE) == pytest.approx(50)
    assert unit_price(market, 7.0 + MARKET_HALF_LIFE) == pytest.approx(MARKET_BASE_PRICE / 1.5)
    assert unit_price(market, 7.0 + 20 * MARKET_HALF_LIFE) == pytest.approx(MARKET_BASE_PRICE)


def test_a_later_sale_starts_from_the_faded_pressure() -> None:
    world = perform(WORLD, SellTokens("a", 100))

    later = perform(replace(world, time=7.0 + MARKET_HALF_LIFE), SellTokens("b", 10))

    assert later.market is not None
    assert later.market.pressure == pytest.approx(60)
    assert later.market.pressure_at == 7.0 + MARKET_HALF_LIFE


def test_one_seller_lowers_the_price_for_everyone() -> None:
    alone = perform(WORLD, SellTokens("b", 50))
    after = perform(perform(WORLD, SellTokens("a", 100)), SellTokens("b", 50))
    assert coins(after, "b") < coins(alone, "b")


@pytest.mark.parametrize("parts", [[1] * 40, [10, 10, 10, 10], [7, 33], [39, 1]])
def test_splitting_a_sale_earns_no_more_than_selling_at_once(parts: list[int]) -> None:
    at_once = coins(perform(WORLD, SellTokens("a", sum(parts))), "a")
    world = WORLD
    for part in parts:
        world = perform(world, SellTokens("a", part))
    split = coins(world, "a")
    assert split <= at_once
    # Only rounding each part down separates the two.
    assert at_once - split < len(parts)


def test_waiting_for_the_price_to_recover_pays_more() -> None:
    hasty = perform(perform(WORLD, SellTokens("a", 100)), SellTokens("a", 100))
    world = perform(WORLD, SellTokens("a", 100))
    patient = perform(replace(world, time=7.0 + 3 * MARKET_HALF_LIFE), SellTokens("a", 100))
    assert coins(patient, "a") > coins(hasty, "a")


def test_the_edge_of_the_radius_is_within_reach() -> None:
    world = replace(WORLD, residents=(resident("a", 55, 50),))
    assert coins(perform(world, SellTokens("a", 1)), "a") > 100


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, SellTokens("c", 1), "not_at_market"),
        (replace(WORLD, market=None), SellTokens("a", 1), "not_at_market"),
        (WORLD, SellTokens("a", 0), "invalid_quantity"),
        (WORLD, SellTokens("a", -3), "invalid_quantity"),
        (WORLD, SellTokens("a", True), "invalid_quantity"),
        (WORLD, SellTokens("a", 1.0), "invalid_quantity"),  # type: ignore[arg-type]
        (WORLD, SellTokens("a", 501), "not_enough_tokens"),
        (WORLD, SellTokens("a", 10**400), "not_enough_tokens"),
        (WORLD, SellTokens("nobody", 1), "unknown_resident"),
        (end_game(WORLD), SellTokens("a", 1), "game_over"),
        (perform(WORLD, LeaveGame("a")), SellTokens("a", 1), "resident_left"),
    ],
)
def test_sales_that_cannot_go_through_change_nothing(
    world: World, action: SellTokens, code: str
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(world, action)
    assert raised.value.code == code


def test_a_sale_worth_less_than_a_coin_is_refused() -> None:
    crowded = replace(WORLD, market=Market(Position(50, 50), 5, pressure=10**6, pressure_at=7.0))
    assert quote(crowded.market, 7.0, 1) == 0  # type: ignore[arg-type]
    with pytest.raises(ActionRejected) as raised:
        perform(crowded, SellTokens("a", 1))
    assert raised.value.code == "price_too_low"


def test_tokens_set_aside_in_an_offer_cannot_be_sold() -> None:
    world = perform(WORLD, OfferToSell("a", "b", TOKEN, 450, 1))

    with pytest.raises(ActionRejected) as raised:
        perform(world, SellTokens("a", 51))

    assert raised.value.code == "not_enough_tokens"


def test_selling_every_token_carried_keeps_a_seller_with_tokens_in_an_offer_in_the_game() -> None:
    world = perform(WORLD, OfferToSell("a", "b", TOKEN, 450, 1))

    sold = perform(world, SellTokens("a", 50))

    assert not sold.residents[0].left


def test_a_busy_resident_cannot_sell() -> None:
    world = replace(
        WORLD,
        residents=(
            replace(
                WORLD.residents[0],
                mining=MiningJob("p1", 20, 5, "MK1"),
                inventory=Inventory(((TOKEN, 500), ("MK1", 1))),
            ),
        ),
    )
    with pytest.raises(ActionRejected) as raised:
        perform(world, SellTokens("a", 1))
    assert raised.value.code == "busy"


def test_selling_every_token_owned_takes_the_seller_out_with_the_coins() -> None:
    world = perform(WORLD, SellTokens("a", 500))
    a = world.residents[0]
    assert a.left
    assert a.coins == 100 + quote(WORLD.market, 7.0, 500)  # type: ignore[arg-type]
    assert item_count(world, "a", TOKEN) == 0


def test_perception_shows_the_market_and_its_price_from_anywhere() -> None:
    world = perform(WORLD, SellTokens("a", 100))

    _, seen = begin_turn(replace(world, time=7.0 + MARKET_HALF_LIFE), "c")

    assert seen.market is not None
    assert seen.market.position == Position(50, 50)
    assert seen.market.radius == 5
    assert seen.market.price == pytest.approx(MARKET_BASE_PRICE / 1.5)


def test_perception_shows_no_market_in_a_world_without_one() -> None:
    _, seen = begin_turn(replace(WORLD, market=None), "c")

    assert seen.market is None


def test_the_default_world_has_a_market_in_the_middle() -> None:
    world = create_default_world(3)
    assert world.market == Market(market_position(), MARKET_RADIUS)
    assert world.market.position == Position(50, 50)


@pytest.mark.parametrize("seed", range(20))
def test_pools_keep_clear_of_the_market(seed: int) -> None:
    world = create_default_world(seed)
    centre = market_position()
    for pool in world.pools:
        distance = math.dist((pool.position.x, pool.position.y), (centre.x, centre.y))
        assert distance >= MARKET_CLEARANCE


def test_mining_and_selling_happen_in_different_places() -> None:
    world = create_default_world(5)
    assert world.market is not None
    for pool in world.pools:
        assert not world.market.reaches(pool.position)
        assert not pool.reaches(world.market.position)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: replace(WORLD, market=Market(Position(101, 50), 5)), "outside the map"),
        (lambda: Market(Position(50, 50), 0), "radius 0"),
        (lambda: Market(Position(50, 50), 5, pressure=-1), "pressure must be a finite number"),
        (
            lambda: Market(Position(50, 50), 5, pressure=math.inf),
            "pressure must be a finite number",
        ),
        (lambda: Market(Position(50, 50), 5, pressure_at=math.nan), "pressure time must be finite"),
        (
            lambda: replace(WORLD, market=Market(Position(50, 50), 5, pressure_at=8.0)),
            "last sold to after the current time",
        ),
    ],
    ids=["off the map", "no radius", "negative", "infinite", "no sale time", "sale in future"],
)
def test_the_world_refuses_an_impossible_market(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()
