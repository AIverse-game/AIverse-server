"""Buying nodes and building ad slots through the agent tools, and what a model is told."""

from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.tool_selection import (
    TOOL_GROUPS,
    discover_tools,
    last_request_tool_names,
    selected_tool_names,
)
from aiverse_server.agents.tools import TOOLS, ResidentTools
from aiverse_server.core.compute import ComputeBook, Job, Node
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.compute import (
    AD_WORKLOAD,
    MAX_NODES,
    NODE_SECONDS,
    SUBSCRIPTION_PRICE,
    on_demand_cost,
    subscription_cost,
)
from aiverse_server.server.app import Game

# a stands in the middle of the map and owns L5-5 (bare) and L1-1 (bare); b owns L2-2 and has
# too few coins for any subscription.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(55, 55), coins=5_000),
        Resident("b", "乙", "#445566", Position(20, 80), coins=10),
    ),
    parcels=(Parcel(5, 5, "a"), Parcel(1, 1, "a"), Parcel(2, 2, "b")),
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


def test_building_on_ones_own_compute_is_free_and_takes_the_workload_over_its_rate() -> None:
    game, _ = make_game()

    built = ResidentTools(game, "a").build_ad("L5-5", "self")

    assert (built.ok, built.message) == (True, "job 1: ad slot on L5-5 ready in 360 s")
    assert game.world.residents[0].coins == 5_000
    assert [job.parcel_id for job in game.world.compute.jobs] == ["L5-5"]


def test_hiring_a_node_for_one_job_costs_coins_and_is_quick() -> None:
    game, _ = make_game()

    built = ResidentTools(game, "a").build_ad("L5-5", "on_demand")

    assert (built.ok, built.message) == (True, "job 1: ad slot on L5-5 ready in 18 s")
    assert game.world.residents[0].coins == 5_000 - on_demand_cost(AD_WORKLOAD)


def test_a_subscription_is_bought_then_used_by_node_id() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    bought = tools.buy_node(NODE_SECONDS[0])
    built = tools.build_ad("L5-5", "subscription", node_id=1)

    assert (bought.ok, bought.message) == (
        True,
        f"subscribed to generative node 1 for {NODE_SECONDS[0]} s",
    )
    assert (built.ok, built.message) == (True, "job 1: ad slot on L5-5 ready in 18 s")
    assert game.world.residents[0].coins == 5_000 - subscription_cost(NODE_SECONDS[0])
    assert tools.calls[0].coins_change == -NODE_SECONDS[0] * SUBSCRIPTION_PRICE
    assert tools.calls[1].coins_change == 0


def test_compute_can_be_started_while_walking() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    tools.move_to(90, 90)

    bought = tools.buy_node(NODE_SECONDS[0])
    built = tools.build_ad("L1-1", "self")

    assert bought.ok and built.ok
    assert game.world.residents[0].walking is not None


def test_a_finished_job_leaves_an_ad_slot_on_the_parcel() -> None:
    game, clock = make_game()
    ResidentTools(game, "a").build_ad("L5-5", "on_demand")

    clock.now = 18.0
    seen = look(game, "a")

    mine = {parcel.id: parcel for parcel in seen.land.mine}
    assert (mine["L5-5"].facility, mine["L5-5"].building_ends_in) == ("ad", None)
    assert seen.compute.my_jobs == ()


def test_refusals_come_back_as_failed_results_and_change_nothing() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    before = game.world

    foreign = tools.build_ad("L2-2", "on_demand")
    no_node = tools.build_ad("L1-1", "subscription", node_id=9)
    short = tools.buy_node(NODE_SECONDS[0] - 1)

    results = [foreign, no_node, short]
    assert [r.code for r in results] == [
        "not_your_parcel",
        "unknown_node",
        "invalid_seconds",
    ]
    assert game.world == before


def test_a_poor_resident_cannot_afford_a_subscription_or_a_hired_node() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "b")

    bought = tools.buy_node(NODE_SECONDS[0])
    hired = tools.build_ad("L2-2", "on_demand")

    assert (bought.code, hired.code) == ("not_enough_coins", "not_enough_coins")


def test_a_node_id_is_needed_for_a_subscription_and_refused_otherwise() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    missing = tools.call("build_ad", {"parcel_id": "L5-5", "source": "subscription"})
    stray = tools.build_ad("L5-5", "self", node_id=1)
    wrong_type = tools.call("build_ad", {"parcel_id": "L5-5", "source": "magic"})

    assert missing.code == "invalid_source"
    assert stray.code == "invalid_source"
    assert wrong_type.code == "invalid_arguments"


def test_tool_descriptions_quote_the_game_constants() -> None:
    descriptions = {tool.name: tool.description for tool in TOOLS}

    assert f"{SUBSCRIPTION_PRICE} coins per second" in descriptions["buy_node"]
    assert str(on_demand_cost(AD_WORKLOAD)) in descriptions["build_ad"]
    assert f"{MAX_NODES} nodes" in descriptions["buy_node"]


def test_the_observation_lists_nodes_jobs_and_what_an_ad_slot_takes() -> None:
    rich = replace(WORLD, residents=(replace(WORLD.residents[0], coins=10_000), WORLD.residents[1]))
    game, clock = make_game(rich)
    tools = ResidentTools(game, "a")
    tools.buy_node(NODE_SECONDS[0])
    tools.buy_node(NODE_SECONDS[0])
    tools.build_ad("L5-5", "subscription", node_id=1)
    tools.build_ad("L1-1", "self")
    clock.now = 2.0

    seen = look(game, "a")

    compute = seen.compute
    assert [(n.node_id, n.working_on, n.free_for, n.expires_in) for n in compute.my_nodes] == [
        (1, 1, 16.0, NODE_SECONDS[0] - 2.0),
        (2, None, 0.0, NODE_SECONDS[0] - 2.0),
    ]
    assert [(j.job_id, j.source, j.ends_in) for j in compute.my_jobs] == [
        (1, "subscription", 16.0),
        (2, "self", 358.0),
    ]
    assert compute.own_job is not None and compute.own_job.parcel == "L1-1"
    assert [(o.source, o.seconds, o.coins) for o in compute.ad_slot] == [
        ("self", 360.0, 0),
        ("on_demand", 18.0, on_demand_cost(AD_WORKLOAD)),
        ("subscription", 18.0, 0),
    ]
    assert (compute.node_coins_per_second, compute.max_nodes) == (SUBSCRIPTION_PRICE, MAX_NODES)
    mine = {parcel.id: parcel.building_ends_in for parcel in seen.land.mine}
    assert mine == {"L5-5": 16.0, "L1-1": 358.0}
    assert '"compute":{"own_job"' in to_json(seen)


def test_the_parcel_underfoot_shows_its_facility_or_the_job_building_it() -> None:
    game, _ = make_game()
    ResidentTools(game, "a").build_ad("L5-5", "self")

    building = look(game, "a").land.here
    other = look(game, "b").land.here

    assert building is not None and (building.facility, building.building_ends_in) == (None, 360.0)
    assert other is not None and other.building_ends_in is None


def test_other_residents_jobs_are_not_shown() -> None:
    game, _ = make_game()
    ResidentTools(game, "a").build_ad("L5-5", "self")

    seen = look(game, "b")

    assert seen.compute.my_jobs == ()
    assert seen.compute.own_job is None


def test_build_ad_is_offered_for_a_bare_parcel_without_a_job_and_buy_node_with_it() -> None:
    game, _ = make_game()

    names = selected_tool_names(look(game, "a"))

    assert {"build_ad", "buy_node"} <= names


def test_build_ad_is_not_offered_when_every_parcel_is_built_or_busy() -> None:
    built = Parcel(5, 5, "a", Facility("ad", 0.0))
    world = World(
        WORLD.map, WORLD.residents, parcels=(built, Parcel(1, 1, "a"), *WORLD.parcels[2:])
    )
    game, _ = make_game(world)
    ResidentTools(game, "a").build_ad("L1-1", "self")

    names = selected_tool_names(look(game, "a"))

    assert "build_ad" not in names


def test_buy_node_is_not_offered_to_a_resident_who_cannot_pay_or_holds_the_most() -> None:
    game, _ = make_game()
    poor = selected_tool_names(look(game, "b"))
    full = World(
        WORLD.map,
        WORLD.residents,
        parcels=WORLD.parcels,
        compute=ComputeBook(
            tuple(Node(n, "a", "generative", 0.0, 500.0) for n in range(1, MAX_NODES + 1)),
            next_node_id=MAX_NODES + 1,
        ),
    )

    holding = selected_tool_names(look(make_game(full)[0], "a"))

    assert "buy_node" not in poor
    assert "build_ad" in holding and "buy_node" not in holding


def test_compute_tools_are_offered_while_walking() -> None:
    game, _ = make_game()
    ResidentTools(game, "a").move_to(90, 90)

    names = selected_tool_names(look(game, "a"))

    assert {"build_ad", "buy_node"} <= names
    assert "move_to" not in names


def test_the_last_request_offers_compute_by_the_live_world() -> None:
    game, _ = make_game()
    busy = World(
        WORLD.map,
        WORLD.residents,
        parcels=WORLD.parcels,
        compute=ComputeBook(
            jobs=(
                Job(1, "a", "build_ad", "L5-5", "self", None, 0.0, 120.0),
                Job(2, "a", "build_ad", "L1-1", "on_demand", None, 0.0, 12.0),
            ),
            next_job_id=3,
        ),
    )

    free = last_request_tool_names(ResidentTools(game, "a"))
    taken = last_request_tool_names(ResidentTools(make_game(busy)[0], "a"))
    poor = last_request_tool_names(ResidentTools(game, "b"))

    assert {"build_ad", "buy_node"} <= free
    assert "build_ad" not in taken
    assert "build_ad" in poor and "buy_node" not in poor


def test_discovering_the_compute_group_reveals_its_tools() -> None:
    names: set[str] = set()

    reply = discover_tools({"group": "compute"}, names)

    assert (
        names
        == set(TOOL_GROUPS["compute"])
        == {
            "buy_node",
            "build_ad",
            "build_venue",
            "commission_build",
        }
    )
    assert '"ok":true' in reply
