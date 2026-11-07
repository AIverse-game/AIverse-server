import json
from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.core.orders import Fill, OrderBook
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.game.actions import MoveTo, OfferToSell
from aiverse_server.server.app import Game

POSTER = "poster"
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident(
            "a",
            "甲",
            "#112233",
            Position(50, 50),
            Inventory(
                ((POSTER, 100),),
            ),
            coins=5000,
        ),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((POSTER, 100),)), coins=5000),
    ),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    # The turn first: it brings the world up to now.
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_observation_shows_self_offers_and_ranking() -> None:
    game, _ = make_game()
    game.act(OfferToSell("b", "a", POSTER, 5, 80))

    seen = look(game, "a")
    them = look(game, "b")

    assert seen.me.items == {POSTER: 100} and seen.me.coins == 5000
    assert seen.offers_to_me[0].from_id == "b" and seen.offers_to_me[0].side == "sell"
    assert [standing.id for standing in seen.residents] == ["a", "b"]
    assert [resident.id for resident in seen.nearby_residents] == ["b"]
    assert them.my_offers[0].to_id == "a"
    assert them.me.items == {POSTER: 95}


def test_observation_shows_a_walk_and_how_long_it_takes() -> None:
    game, _ = make_game()
    game.act(MoveTo("a", Position(70, 50)))

    seen = look(game, "a")

    assert seen.me.walking_to == Position(70, 50)
    assert seen.me.arrives_in == 8.0
    assert seen.land.here is None


def test_observation_reports_how_long_earlier_rounds_took() -> None:
    game, _ = make_game()
    perception = game.begin_turn("a")

    first = observe(game.world, "a", perception, game.remaining())
    later = observe(game.world, "a", perception, game.remaining(), (30.0, 90.0))

    assert (first.last_thinking_seconds, first.avg_thinking_seconds) == (None, None)
    assert (later.last_thinking_seconds, later.avg_thinking_seconds) == (90.0, 60.0)
    assert json.loads(to_json(later))["avg_thinking_seconds"] == 60.0


def test_exchange_history_is_bounded_and_does_not_reveal_parties() -> None:
    fills = tuple(Fill(i, float(i * 40), POSTER, 1, 500 + i, "a", "b", "a") for i in range(1, 12))
    game, clock = make_game(replace(WORLD, orders=OrderBook(fills=fills, next_seq=12)))
    clock.now = 440.0

    seen = look(game, "a")
    public_trades = json.loads(to_json(seen))["exchange"]["recent_trades"]

    assert len(public_trades) == 8
    assert public_trades[0]["price"] == 504
    assert public_trades[-1]["ago"] == 0
    assert all("buyer_id" not in trade and "seller_id" not in trade for trade in public_trades)


def test_observation_json_is_compact_and_rounded() -> None:
    game, clock = make_game()

    clock.now = 1 / 3
    shown = json.loads(to_json(look(game, "a")))

    assert shown["time"] == 0.3
    assert shown["me"]["position"] == {"x": 50, "y": 50}
