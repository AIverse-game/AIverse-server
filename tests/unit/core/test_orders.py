from collections.abc import Callable

import pytest

from aiverse_server.core.orders import FILL_LIMIT, Fill, Order, OrderBook
from aiverse_server.core.world import Position, Resident, World, WorldMap

ORDER = Order(1, "a", "buy", "data", 5, 10, 0, 300)


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: Order(1, "a", "buy", "data", 0, 10, 0, 300), "quantity"),
        (lambda: Order(1, "a", "buy", "data", 5, 0, 0, 300), "price"),
        (lambda: Order(1, "a", "buy", "data", 5, 10, 50, 40), "expires before"),
        (lambda: OrderBook((ORDER, ORDER), 2), "unique"),
        (lambda: OrderBook((ORDER,), 1), "already issued"),
        (
            lambda: OrderBook(fills=(Fill(1, 0, "data", 1, 1, "a", "b", "a"),) * (FILL_LIMIT + 1)),
            "at most",
        ),
        (lambda: OrderBook(fills=(Fill(3, 0, "data", 1, 1, "a", "b", "a"),), next_seq=2), "fill"),
    ],
    ids=["quantity", "price", "expiry", "duplicate", "unissued", "too_many_fills", "unissued_fill"],
)
def test_impossible_orders_and_books_are_rejected(make: Callable[[], object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make()


@pytest.mark.parametrize(("left", "ended"), [(True, False), (False, True)], ids=["left", "ended"])
def test_the_world_rejects_orders_outliving_their_resident_or_game(left: bool, ended: bool) -> None:
    owner = Resident("a", "a", "#123456", Position(1, 1), left=left)

    with pytest.raises(ValueError, match="outlives"):
        World(WorldMap(10), (owner,), ended=ended, orders=OrderBook((ORDER,), 2))
