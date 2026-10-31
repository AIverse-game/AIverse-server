import json

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.runner import TurnRecord, record_to_json
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
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


def test_records_are_standard_json_even_with_nan_arguments() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    tools.call("move_to", {"x": float("nan"), "y": 1})
    turn = TurnRecord("a", 0.0, to_json(look(game, "a")), tuple(tools.calls))
    line = record_to_json(turn)
    assert json.loads(line)["calls"][0]["arguments"]["x"] == "nan"
