"""The exchange through the agent tools, and what the observation tells a model about it."""

from aiverse_server.agents.observation import (
    BookView,
    MyFillView,
    MyOrderView,
    Observation,
    observe,
)
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Position, Resident, World, WorldMap
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.shards import SHARD
from aiverse_server.server.app import Game

STOCK = Inventory(((ENERGY, 100), (SHARD, 50)))
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), STOCK, coins=1_000),
        Resident("b", "乙", "#445566", Position(50, 50), STOCK, coins=1_000),
    ),
    market=Market(Position(50, 50), 5),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game() -> Game:
    return Game(WORLD, 600, FakeClock())


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_place_order_reports_an_order_left_resting() -> None:
    game = make_game()

    result = ResidentTools(game, "a").place_order("sell", SHARD, 10, 20)

    assert (result.ok, result.message) == (True, "order 1: 10 resting at 20 each")


def test_place_order_reports_what_filled_and_what_rests() -> None:
    game = make_game()
    ResidentTools(game, "a").place_order("sell", SHARD, 4, 20)

    result = ResidentTools(game, "b").place_order("buy", SHARD, 10, 25)

    assert result.message == "filled 4 for 80 coins; order 2: 6 resting at 25 each"


def test_selling_all_energy_in_hand_on_the_exchange_is_refused() -> None:
    game = make_game()

    result = ResidentTools(game, "a").place_order("sell", ENERGY, 100, 20)

    assert (result.ok, result.code) == (False, "last_energy")
    assert game.world == WORLD


def test_cancel_order_hands_back_what_the_order_holds() -> None:
    game = make_game()
    tools = ResidentTools(game, "a")
    tools.place_order("buy", SHARD, 10, 20)

    result = tools.cancel_order(1)

    assert result.ok and result.status is not None and result.status["coins"] == 1_000


def test_the_observation_shows_each_book_without_names() -> None:
    game = make_game()
    ResidentTools(game, "a").place_order("sell", SHARD, 4, 20)
    ResidentTools(game, "b").place_order("buy", SHARD, 6, 18)

    seen = look(game, "b")

    shards = next(book for book in seen.exchange.books if book.item == SHARD)
    assert shards == BookView(SHARD, ((18, 6),), ((20, 4),), None, 0)


def test_the_observation_lists_ones_own_orders_and_fills() -> None:
    game = make_game()
    ResidentTools(game, "a").place_order("sell", SHARD, 10, 20)
    ResidentTools(game, "b").place_order("buy", SHARD, 4, 20)

    seen = look(game, "a")

    assert seen.exchange.my_orders == (MyOrderView(1, "sell", SHARD, 6, 20, 300),)
    assert seen.exchange.my_fills == (MyFillView(0, "sold", SHARD, 4, 20),)


def test_the_observation_counts_coins_set_aside_in_buy_orders() -> None:
    game = make_game()
    ResidentTools(game, "b").place_order("buy", SHARD, 6, 18)

    seen = look(game, "b")

    assert (seen.me.coins, seen.me.coins_in_orders) == (1_000 - 108, 108)
