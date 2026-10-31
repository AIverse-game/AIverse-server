import json

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import OfferToSell
from aiverse_server.server.app import Game

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((TOKEN, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((TOKEN, 100),)), coins=5000),
    ),
    pools=(Pool("p1", Position(80, 50), 4, 300),),
    market=Market(Position(50, 50), 5),
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


def test_observation_shows_self_market_pools_offers_and_ranking() -> None:
    game, _ = make_game()
    game.act(OfferToSell("b", "a", TOKEN, 5, 80))

    seen = look(game, "a")
    them = look(game, "b")

    assert seen.me.at_market and seen.me.tokens == 100 and seen.me.coins == 5000
    assert seen.market is not None and seen.market.distance == 0
    assert seen.offers_to_me[0].from_id == "b" and seen.offers_to_me[0].side == "sell"
    assert [standing.id for standing in seen.residents] == ["a", "b"]
    assert [resident.id for resident in seen.nearby_residents] == ["b"]
    # The pool is 30 units away, out of sight, so not yet known.
    assert seen.known_pools == ()
    assert them.my_offers[0].to_id == "a"
    assert them.me.tokens == 95 and them.me.tokens_in_offers == 5
    assert [pool.id for pool in them.known_pools] == ["p1"]
    assert them.known_pools[0].walk_cost == 20


def test_observation_json_is_compact_and_rounded() -> None:
    game, clock = make_game()

    clock.now = 1 / 3
    shown = json.loads(to_json(look(game, "a")))

    assert shown["time"] == 0.3
    assert shown["me"]["position"] == {"x": 50, "y": 50}
