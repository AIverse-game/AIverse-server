"""Buying land through the agent tools, and what the observation tells a model about land."""

from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tool_selection import last_request_tool_names, selected_tool_names
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Parcel
from aiverse_server.core.visitors import Crowd, Visitor
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.sponsors import default_sponsors
from aiverse_server.server.app import Game

# a stands in the free parcel L5-3 north of the middle of the map with plenty of coins; b has
# too few for any parcel.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(55, 35), coins=5_000),
        Resident("b", "乙", "#445566", Position(20, 80), coins=10),
    ),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD) -> Game:
    return Game(world, 600, FakeClock())


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def standing(number: int, x: float, y: float) -> Visitor:
    """A visitor in town standing at (x, y) for a long while."""
    spot = Position(x, y)
    return Visitor(number, "staying", 1_000.0, spot, spot)


def test_the_observation_prices_the_parcel_underfoot_and_counts_the_people_passing_it() -> None:
    crowd = Crowd(visitors=(standing(0, 55, 35), standing(1, 60, 38), standing(2, 5, 95)))
    game = make_game(replace(WORLD, crowd=crowd, sponsors=default_sponsors()))

    seen = look(game, "a")

    here = seen.land.here
    assert here is not None
    assert (here.id, here.price, here.owner) == ("L5-3", 2_540, None)
    # Two of the three visitors are within reach; each stands for one person at the start.
    assert here.people_near == 2.0
    assert [(s.name, s.price_per_view) for s in seen.land.sponsors] == [
        ("星河饮品", 2.4),
        ("云端出行", 1.8),
        ("拾光书店", 1.2),
    ]


def test_the_best_free_parcels_come_first_by_the_people_passing() -> None:
    crowd = Crowd(visitors=(standing(0, 55, 35), standing(1, 85, 85), standing(2, 86, 84)))
    game = make_game(replace(WORLD, crowd=crowd))

    best = look(game, "b").land.best_free

    passing = [parcel.people_near for parcel in best]
    assert len(best) == 5
    assert passing == sorted(passing, reverse=True)
    assert passing[0] == 2.0
    assert all(parcel.owner is None for parcel in best)


def test_a_resident_buys_the_parcel_underfoot_and_it_is_listed_as_theirs() -> None:
    game = make_game()

    bought = ResidentTools(game, "a").buy_parcel()
    seen = look(game, "a")

    assert (bought.ok, bought.message) == (True, "bought parcel L5-3")
    assert [parcel.id for parcel in seen.land.mine] == ["L5-3"]
    assert seen.me.coins == 5_000 - 2_540
    assert [parcel.owner_id for parcel in game.world.parcels] == ["a"]


def test_buying_land_without_the_coins_is_refused() -> None:
    game = make_game()

    result = ResidentTools(game, "b").buy_parcel()

    assert (result.ok, result.code) == (False, "not_enough_coins")
    assert game.world.parcels == ()


def test_buy_parcel_is_offered_only_where_the_resident_can_afford_a_free_parcel() -> None:
    game = make_game()
    ResidentTools(game, "a").buy_parcel()

    afford = selected_tool_names(look(make_game(), "a"))
    owned = selected_tool_names(look(game, "a"))
    poor = selected_tool_names(look(game, "b"))

    assert "buy_parcel" in afford
    assert "buy_parcel" not in owned and "buy_parcel" not in poor


def test_the_last_request_offers_buy_parcel_by_the_live_world() -> None:
    owned = make_game(World(map=WORLD.map, residents=WORLD.residents, parcels=(Parcel(5, 3, "b"),)))

    free = last_request_tool_names(ResidentTools(make_game(), "a"))
    taken = last_request_tool_names(ResidentTools(owned, "a"))

    assert "buy_parcel" in free and "buy_parcel" not in taken
