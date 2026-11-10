"""Building game halls and workspaces, rendering games and showing them through the agent
tools, and what a model is told about them."""

from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.tool_selection import (
    last_request_tool_names,
    selected_tool_names,
)
from aiverse_server.agents.tools import TOOLS, ResidentTools
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.ads import CONTENT_ITEM, GAME_ITEM, GAME_LIFETIME
from aiverse_server.economy.compute import (
    GAME_WORKLOAD,
    SELF_RATE,
    VENUE_WORKLOAD,
    on_demand_cost,
)
from aiverse_server.server.app import Game

# a owns a game hall on L7-7 and a bare L1-1, and carries a game and an ad; b owns nothing.
HALL = Facility("game_hall", 0.0)
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        Resident(
            "a",
            "甲",
            "#112233",
            Position(75, 75),
            Inventory(((CONTENT_ITEM, 1), (GAME_ITEM, 1))),
            coins=5_000,
        ),
        Resident("b", "乙", "#445566", Position(20, 80), coins=5_000),
    ),
    parcels=(Parcel(7, 7, "a", HALL), Parcel(1, 1, "a")),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, 600, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_a_venue_is_built_on_the_own_compute_and_appears_when_the_job_ends() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "a")

    built = tools.build_venue("L1-1", "workspace", "self")
    clock.now = VENUE_WORKLOAD / SELF_RATE
    game.tick()

    assert (built.ok, built.message) == (True, "job 1: workspace on L1-1 ready in 480 s")
    facility = next(p.facility for p in game.world.parcels if p.id == "L1-1")
    assert facility is not None and facility.kind == "workspace"


def test_a_game_hall_is_built_on_a_hired_node_for_coins() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    built = tools.build_venue("L1-1", "game_hall", "on_demand")

    assert (built.ok, built.message) == (True, "job 1: game hall on L1-1 ready in 24 s")
    assert game.world.residents[0].coins == 5_000 - on_demand_cost(VENUE_WORKLOAD)


def test_a_venue_is_refused_on_a_built_foreign_or_unknown_parcel_and_for_an_unknown_kind() -> None:
    game, _ = make_game()
    mine = ResidentTools(game, "a")
    theirs = ResidentTools(game, "b")

    built = mine.build_venue("L7-7", "workspace", "self")
    foreign = theirs.build_venue("L1-1", "workspace", "self")
    casino = mine.call("build_venue", {"parcel_id": "L1-1", "kind": "casino", "source": "self"})

    assert (built.code, foreign.code, casino.code) == (
        "parcel_built",
        "not_your_parcel",
        "invalid_arguments",
    )


def test_a_game_is_rendered_and_goes_into_the_hall() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "b")

    made = tools.make_content("self", content="game")
    clock.now = GAME_WORKLOAD / SELF_RATE
    game.tick()

    assert (made.ok, made.message) == (True, "job 1: a game ready in 180 s")
    assert game.world.residents[1].inventory.count(GAME_ITEM) == 1


def test_a_game_goes_into_a_hall_and_an_ad_is_not_taken_for_it() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    placed = tools.place_content("L7-7")

    assert (placed.ok, placed.message) == (True, "the game hall on L7-7 now shows 1")
    assert game.world.residents[0].inventory.count(GAME_ITEM) == 0
    assert game.world.residents[0].inventory.count(CONTENT_ITEM) == 1


def test_placing_is_refused_in_a_workspace_and_without_the_matching_item() -> None:
    workspace = World(
        WORLD.map,
        WORLD.residents,
        parcels=(Parcel(7, 7, "a", Facility("workspace", 0.0)), Parcel(1, 1, "a", HALL)),
    )
    ads_only = replace(WORLD.residents[0], inventory=Inventory(((CONTENT_ITEM, 1),)))
    no_games = World(WORLD.map, (ads_only, WORLD.residents[1]), parcels=WORLD.parcels)

    in_workspace = ResidentTools(make_game(workspace)[0], "a").place_content("L7-7")
    ad_for_hall = ResidentTools(make_game(no_games)[0], "a").place_content("L7-7")

    assert in_workspace.code == "no_content_facility"
    assert ad_for_hall.code == "not_enough_items"


def test_the_observation_shows_the_hall_its_games_and_what_venues_and_games_cost() -> None:
    game, clock = make_game()
    ResidentTools(game, "a").place_content("L7-7")
    clock.now = 100.0

    seen = look(game, "a")

    hall = next(parcel for parcel in seen.land.mine if parcel.id == "L7-7")
    assert (hall.facility, hall.content_goes_stale_in, hall.income_last_payday) == (
        "game_hall",
        (GAME_LIFETIME - 100.0,),
        0,
    )
    assert [(o.source, o.seconds) for o in seen.compute.venue] == [
        ("self", 480.0),
        ("on_demand", 24.0),
        ("subscription", 24.0),
    ]
    assert [(o.source, o.seconds) for o in seen.compute.game] == [
        ("self", 180.0),
        ("on_demand", 9.0),
        ("subscription", 9.0),
    ]
    assert '"game":[{"source":"self"' in to_json(seen)


def test_building_a_venue_is_offered_with_a_bare_parcel_and_placing_with_a_matching_item() -> None:
    game, _ = make_game()

    owner = selected_tool_names(look(game, "a"))
    stranger = selected_tool_names(look(game, "b"))

    assert {"build_venue", "place_content"} <= owner
    assert "build_venue" not in stranger and "place_content" not in stranger


def test_placing_is_not_offered_to_someone_carrying_only_a_kind_no_facility_of_theirs_takes() -> (
    None
):
    carrying_ads = replace(WORLD.residents[0], inventory=Inventory(((CONTENT_ITEM, 1),)))
    ads_only = World(WORLD.map, (carrying_ads, WORLD.residents[1]), parcels=WORLD.parcels)
    game, _ = make_game(ads_only)

    advertised = selected_tool_names(look(game, "a"))
    last = last_request_tool_names(ResidentTools(game, "a"))

    assert "place_content" not in advertised and "place_content" not in last


def test_the_last_request_offers_venues_and_placing_by_the_live_world() -> None:
    game, _ = make_game()

    owner = last_request_tool_names(ResidentTools(game, "a"))
    stranger = last_request_tool_names(ResidentTools(game, "b"))

    assert {"build_venue", "place_content"} <= owner
    assert "build_venue" not in stranger and "place_content" not in stranger


def test_tool_descriptions_quote_the_game_constants() -> None:
    descriptions = {tool.name: tool.description for tool in TOOLS}

    assert str(on_demand_cost(VENUE_WORKLOAD)) in descriptions["build_venue"]
    assert str(on_demand_cost(GAME_WORKLOAD)) in descriptions["make_content"]
    assert GAME_ITEM in descriptions["place_content"]


def test_a_parcel_a_venue_takes_up_is_shown_as_covered_and_leaves_no_room_for_another_venue() -> (
    None
):
    blocks = replace(
        WORLD,
        map=WorldMap(size=90, block_cells=3, venue_cells=2),
        parcels=(
            *(Parcel(col, row, "a") for row in range(2) for col in range(2)),
            Parcel(2, 2, "a"),
        ),
    )
    game, _ = make_game(blocks)
    ResidentTools(game, "a").build_venue("L0-0", "workspace", "self")

    seen = look(game, "a")

    mine = {parcel.id: parcel for parcel in seen.land.mine}
    assert [mine[id_].covered_by for id_ in ("L0-0", "L1-0", "L0-1", "L1-1")] == [
        None,
        "L0-0",
        "L0-0",
        "L0-0",
    ]
    assert not any(parcel.venue_room or parcel.mall_orientations for parcel in seen.land.mine)
    names = selected_tool_names(seen)
    assert "build_ad" in names and "build_venue" not in names
    assert "build_venue" not in last_request_tool_names(ResidentTools(game, "a"))
