"""Buying land through the agent tools, and what the observation tells a model about land."""

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tool_selection import last_request_tool_names, selected_tool_names
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.footfall import footfall, visitors
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


def test_the_observation_prices_the_parcel_underfoot_and_shows_its_footfall() -> None:
    game = make_game()

    seen = look(game, "a")

    here = seen.land.here
    assert here is not None
    assert (here.id, here.price, here.owner) == ("L5-3", 2_540, None)
    # 84.2 visitors this far out, plus a standing there.
    assert here.footfall == footfall(game.world, here.centre)
    assert round(here.footfall, 1) == 94.2
    assert seen.land.visitors_now == visitors(0.0)


def test_the_best_free_parcels_come_first_by_footfall() -> None:
    game = make_game()

    best = look(game, "b").land.best_free

    footfalls = [parcel.footfall for parcel in best]
    assert len(best) == 5
    assert footfalls == sorted(footfalls, reverse=True)
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
