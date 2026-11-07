"""Buying land, making materials and building ad slots through the agent tools, and what the
observation tells a model about land."""

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tool_selection import selected_tool_names
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    Resident,
    Studio,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.facilities import AD_MATERIALS, FACILITY_SECONDS
from aiverse_server.economy.gathering import DATA
from aiverse_server.economy.studio import MATERIAL
from aiverse_server.server.app import Game

# a stands in the free parcel L5-3 north of the market with coins and materials for an ad;
# b stands at the studio with data for a batch of materials.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident(
            "a",
            "甲",
            "#112233",
            Position(55, 35),
            Inventory(((COMPUTE, 100), (MATERIAL, AD_MATERIALS))),
            coins=5_000,
        ),
        Resident(
            "b",
            "乙",
            "#445566",
            Position(20, 80),
            Inventory(((COMPUTE, 100), (DATA, 10))),
            coins=10,
        ),
    ),
    market=Market(Position(50, 50), 5),
    studio=Studio(Position(20, 80), 5),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game() -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(WORLD, 600, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_the_observation_prices_the_parcel_underfoot_and_what_an_ad_there_would_earn() -> None:
    game, _ = make_game()

    seen = look(game, "a")

    here = seen.land.here
    assert here is not None
    assert (here.id, here.price, here.owner, here.facility) == ("L5-3", 2_540, None, None)
    # 84.2 visitors this far out plus a standing there, at 2 coins each.
    assert here.ad_income == 188
    assert seen.me.materials == AD_MATERIALS


def test_the_best_free_parcels_come_first_and_leave_out_reserved_land() -> None:
    game, _ = make_game()

    best = look(game, "b").land.best_free

    incomes = [parcel.ad_income or 0 for parcel in best]
    assert len(best) == 5
    assert incomes == sorted(incomes, reverse=True)
    assert not {"L4-4", "L4-5", "L5-4", "L5-5", "L1-7", "L1-8", "L2-7", "L2-8"} & {
        parcel.id for parcel in best
    }


def test_a_resident_buys_land_and_builds_an_ad_that_then_pays_them() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "a")

    bought = tools.buy_parcel()
    built = tools.build_ad()
    clock.now = FACILITY_SECONDS
    game.tick()
    seen = look(game, "a")

    assert (bought.ok, bought.message) == (True, "bought parcel L5-3")
    assert built.ok and built.message.startswith("built an ad slot on L5-3")
    (mine,) = seen.land.mine
    # Visitors have grown to 102.5 by the first payout.
    assert (mine.id, mine.facility, mine.last_income, mine.earned) == ("L5-3", "ad", 192, 192)
    assert seen.me.coins == 5_000 - 2_540 + 192


def test_make_material_starts_a_job_shown_in_the_status() -> None:
    game, _ = make_game()

    result = ResidentTools(game, "b").make_material()

    assert result.ok
    assert result.status is not None and result.status["writing_program"] == MATERIAL


def test_land_tools_are_offered_where_they_can_be_used() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    before = selected_tool_names(look(game, "a"))
    tools.buy_parcel()
    after = selected_tool_names(look(game, "a"))
    at_studio = selected_tool_names(look(game, "b"))

    assert "buy_parcel" in before and "build_ad" not in before
    assert "build_ad" in after and "buy_parcel" not in after
    assert {"make_material", "buy_parcel"} & at_studio == {"make_material"}
