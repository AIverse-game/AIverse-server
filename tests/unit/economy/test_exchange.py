from dataclasses import replace

import pytest

from aiverse_server.core.inventory import item_count
from aiverse_server.core.ledger import Transfer
from aiverse_server.core.orders import FILL_LIMIT, Fill, Order, OrderBook
from aiverse_server.core.world import Inventory, Market, Position, Resident, World, WorldMap
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.exchange import (
    MAX_ORDERS,
    depth,
    last_price,
    next_order_expiry,
    volume_since,
)
from aiverse_server.economy.gathering import DATA
from aiverse_server.game.actions import (
    ActionRejected,
    CancelOrder,
    LeaveGame,
    MoveTo,
    PlaceOrder,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game, total_coins


def resident(resident_id: str, x: float = 50) -> Resident:
    stock = Inventory(((COMPUTE, 100), (DATA, 50)))
    return Resident(resident_id, resident_id, "#123456", Position(x, 50), stock, coins=1_000)


# a, b, c and e stand at the market, d far from it.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a"), resident("b"), resident("c"), resident("d", x=10), resident("e")),
    market=Market(Position(50, 50), 5),
)


def run(world: World, *actions: PlaceOrder | CancelOrder | MoveTo | LeaveGame) -> World:
    for action in actions:
        world = perform(world, action)
    return world


def coins(world: World, resident_id: str) -> int:
    return next(r.coins for r in world.residents if r.id == resident_id)


def test_a_buy_order_rests_with_its_coins_set_aside() -> None:
    world = perform(WORLD, PlaceOrder("a", "buy", DATA, 10, 20))

    assert world.orders.orders == (Order(1, "a", "buy", DATA, 10, 20, 0, 300),)
    assert coins(world, "a") == 1_000 - 200
    assert total_coins(world, "a") == 1_000


def test_a_sell_order_rests_with_its_items_set_aside() -> None:
    world = perform(WORLD, PlaceOrder("a", "sell", DATA, 10, 20))

    assert item_count(world, "a", DATA) == 40
    assert depth(world, DATA, "sell", 3) == ((20, 10),)


def test_a_buy_fills_at_the_resting_price_and_gets_the_difference_back() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 10, 20), PlaceOrder("b", "buy", DATA, 10, 25))

    assert (coins(world, "a"), coins(world, "b")) == (1_200, 800)
    assert (item_count(world, "a", DATA), item_count(world, "b", DATA)) == (40, 60)
    assert world.orders.orders == ()


def test_a_sell_fills_at_the_resting_buy_price() -> None:
    world = run(WORLD, PlaceOrder("a", "buy", DATA, 10, 25), PlaceOrder("b", "sell", DATA, 10, 20))

    assert (coins(world, "a"), coins(world, "b")) == (750, 1_250)


def test_the_best_price_fills_first_then_the_oldest_at_that_price() -> None:
    world = run(
        WORLD,
        PlaceOrder("a", "sell", DATA, 5, 22),
        PlaceOrder("b", "sell", DATA, 5, 20),
        PlaceOrder("c", "sell", DATA, 5, 20),
    )

    filled = perform(world, PlaceOrder("e", "buy", DATA, 7, 30))

    sellers = [(fill.seller_id, fill.quantity, fill.price) for fill in filled.orders.fills]
    assert sellers == [("b", 5, 20), ("c", 2, 20)]


def test_what_is_left_of_a_partly_filled_order_rests() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 4, 20), PlaceOrder("b", "buy", DATA, 10, 20))

    assert depth(world, DATA, "buy", 3) == ((20, 6),)
    assert coins(world, "b") == 1_000 - 200


def test_an_order_placed_far_from_the_market_trades() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 4, 20), PlaceOrder("d", "buy", DATA, 4, 20))

    assert item_count(world, "d", DATA) == 54
    assert coins(world, "a") == 1_000 + 80


def test_an_order_cannot_be_placed_while_walking() -> None:
    world = perform(WORLD, MoveTo("d", Position(30, 50)))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, PlaceOrder("d", "buy", DATA, 1, 1))

    assert rejected.value.code == "busy"


def test_a_fill_is_logged_without_names_for_others_and_paid_through_the_ledger() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 4, 20), PlaceOrder("b", "buy", DATA, 4, 25))

    assert world.orders.fills == (Fill(1, 0, DATA, 4, 20, "b", "a", "b"),)
    assert world.ledger.transfers == (Transfer(0, "b", "a", 80, "exchange", DATA, 4),)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (PlaceOrder("a", "buy", "gold", 1, 1), "unknown_item"),
        (PlaceOrder("a", "buy", DATA, 0, 1), "invalid_order"),
        (PlaceOrder("a", "buy", DATA, 1, 0), "invalid_order"),
        (PlaceOrder("a", "buy", DATA, 1, 1, 10), "invalid_order"),
        (PlaceOrder("a", "buy", DATA, 100, 11), "not_enough_coins"),
        (PlaceOrder("a", "sell", DATA, 51, 1), "not_enough_items"),
    ],
    ids=["item", "quantity", "price", "lifetime", "coins", "items"],
)
def test_an_order_that_cannot_be_placed_is_refused(action: PlaceOrder, code: str) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, action)

    assert rejected.value.code == code


def test_an_order_crossing_ones_own_resting_order_is_refused() -> None:
    world = perform(WORLD, PlaceOrder("a", "sell", DATA, 5, 20))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, PlaceOrder("a", "buy", DATA, 5, 20))

    assert rejected.value.code == "self_cross"


def test_a_buy_across_several_prices_gets_back_the_difference_at_each() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 2, 20), PlaceOrder("b", "sell", DATA, 3, 22))

    filled = perform(world, PlaceOrder("c", "buy", DATA, 5, 25))

    # 2 at 20 and 3 at 22 cost 106 of the 125 set aside; 19 come back.
    assert coins(filled, "c") == 1_000 - 106


def test_with_the_most_orders_resting_an_order_that_fills_at_once_is_still_taken() -> None:
    full = run(WORLD, *(PlaceOrder("a", "buy", DATA, 1, 1) for _ in range(MAX_ORDERS)))
    world = perform(full, PlaceOrder("b", "buy", COMPUTE, 5, 10))

    filled = perform(world, PlaceOrder("a", "sell", COMPUTE, 5, 10))

    assert item_count(filled, "b", COMPUTE) == 105


def test_a_resident_may_rest_only_so_many_orders() -> None:
    world = run(WORLD, *(PlaceOrder("a", "buy", DATA, 1, 1) for _ in range(MAX_ORDERS)))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, PlaceOrder("a", "buy", DATA, 1, 1))

    assert rejected.value.code == "too_many_orders"


def test_cancelling_hands_back_what_the_order_still_holds() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 10, 20), PlaceOrder("b", "buy", DATA, 4, 20))

    cancelled = perform(world, CancelOrder("a", 1))

    assert item_count(cancelled, "a", DATA) == 46
    assert cancelled.orders.orders == ()


def test_an_order_can_be_cancelled_from_afar_while_walking() -> None:
    world = run(WORLD, PlaceOrder("a", "buy", DATA, 10, 20), MoveTo("a", Position(90, 90)))

    cancelled = perform(world, CancelOrder("a", 1))

    assert coins(cancelled, "a") == 1_000


@pytest.mark.parametrize(("resident_id", "order_id"), [("a", 9), ("b", 1)], ids=["none", "not_own"])
def test_cancelling_an_order_one_does_not_have_is_refused(resident_id: str, order_id: int) -> None:
    world = perform(WORLD, PlaceOrder("a", "buy", DATA, 1, 1))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, CancelOrder(resident_id, order_id))

    assert rejected.value.code == "unknown_order"


def test_an_order_expires_and_hands_back_what_it_holds() -> None:
    world = perform(WORLD, PlaceOrder("a", "buy", DATA, 10, 20, 30))

    expired = settle_due(replace(world, time=30))

    assert (next_order_expiry(world), next_order_expiry(expired)) == (30, None)
    assert coins(expired, "a") == 1_000


def test_leaving_withdraws_ones_orders_before_the_items_are_cleared() -> None:
    world = run(WORLD, PlaceOrder("a", "buy", DATA, 10, 20), PlaceOrder("b", "sell", DATA, 5, 30))

    left = perform(world, LeaveGame("a"))

    assert coins(left, "a") == 1_000
    assert [order.resident_id for order in left.orders.orders] == ["b"]


def test_a_seller_bought_out_of_compute_gets_their_other_orders_back() -> None:
    world = run(
        WORLD, PlaceOrder("a", "sell", COMPUTE, 100, 5), PlaceOrder("a", "buy", DATA, 10, 20)
    )

    filled = perform(world, PlaceOrder("b", "buy", COMPUTE, 100, 5))

    assert [order.resident_id for order in filled.orders.orders] == []
    assert coins(filled, "a") == 1_000 + 500


def test_the_game_ending_withdraws_every_order() -> None:
    world = run(WORLD, PlaceOrder("a", "buy", DATA, 10, 20), PlaceOrder("b", "sell", DATA, 5, 30))

    ended = end_game(world)

    assert ended.orders.orders == ()
    assert (coins(ended, "a"), item_count(ended, "b", DATA)) == (1_000, 50)


def test_compute_resting_in_a_sell_order_is_still_owned() -> None:
    world = perform(WORLD, PlaceOrder("a", "sell", COMPUTE, 100, 30))

    assert not next(r for r in world.residents if r.id == "a").left


def test_selling_ones_last_compute_on_the_exchange_takes_the_seller_out() -> None:
    world = perform(WORLD, PlaceOrder("a", "sell", COMPUTE, 100, 5))

    filled = perform(world, PlaceOrder("b", "buy", COMPUTE, 100, 5))

    assert next(r for r in filled.residents if r.id == "a").left


def test_the_book_keeps_only_the_latest_fills() -> None:
    fills = tuple(Fill(seq, 0, DATA, 1, 1, "a", "b", "a") for seq in range(1, FILL_LIMIT + 1))
    resting = Order(1, "a", "sell", DATA, 1, 5, 0, 300)
    world = replace(WORLD, orders=OrderBook((resting,), 2, fills, FILL_LIMIT + 1))

    filled = perform(world, PlaceOrder("b", "buy", DATA, 1, 5))

    assert len(filled.orders.fills) == FILL_LIMIT
    assert filled.orders.fills[-1].seq == FILL_LIMIT + 1


def test_the_book_tells_the_last_price_and_recent_volume() -> None:
    world = run(WORLD, PlaceOrder("a", "sell", DATA, 4, 20), PlaceOrder("b", "buy", DATA, 4, 25))
    later = replace(world, time=200)

    assert (last_price(WORLD, DATA), last_price(later, DATA)) == (None, 20)
    assert (volume_since(later, DATA, 0), volume_since(later, DATA, 80)) == (4, 0)
