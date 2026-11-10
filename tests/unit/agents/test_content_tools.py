"""Making content, putting it in ad slots and trading it through the agent tools, and what a
model is told about it."""

from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.tool_selection import (
    TOOL_GROUPS,
    discover_tools,
    last_request_tool_names,
    selected_tool_names,
)
from aiverse_server.agents.tools import TOOLS, ResidentTools
from aiverse_server.core.land import AD_SLOT_CAPACITY, Facility, Parcel
from aiverse_server.core.sponsors import Sponsor, SponsorBook
from aiverse_server.core.visitors import Crowd
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.ads import CONTENT_ITEM, CONTENT_LIFETIME
from aiverse_server.economy.compute import (
    CONTENT_WORKLOAD,
    NODE_SECONDS,
    SELF_RATE,
    on_demand_cost,
    subscription_cost,
)
from aiverse_server.economy.sponsors import PAY_PERIOD
from aiverse_server.server.app import Game

# a owns L7-7 with an ad slot at the middle of the 150 map and carries two pieces of content;
# b owns nothing and carries none.
SLOT = Facility("ad", 0.0)
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        Resident(
            "a", "甲", "#112233", Position(75, 75), Inventory(((CONTENT_ITEM, 2),)), coins=5_000
        ),
        Resident("b", "乙", "#445566", Position(20, 80), coins=5_000),
    ),
    parcels=(Parcel(7, 7, "a", SLOT), Parcel(1, 1, "a")),
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


def test_content_is_made_on_the_own_compute_for_free_and_arrives_in_the_inventory() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "b")

    made = tools.make_content("self")
    clock.now = CONTENT_WORKLOAD / SELF_RATE
    game.tick()

    assert (made.ok, made.message) == (True, "job 1: a piece of content ready in 120 s")
    assert game.world.residents[1].coins == 5_000
    assert game.world.residents[1].inventory.count(CONTENT_ITEM) == 1


def test_a_hired_node_costs_coins_and_a_subscribed_render_node_is_named_by_id() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    hired = tools.make_content("on_demand")
    bought = tools.buy_node(NODE_SECONDS[0], "render")
    subscribed = tools.make_content("subscription", node_id=1)

    assert (hired.ok, hired.message) == (True, "job 1: a piece of content ready in 6 s")
    assert (bought.ok, bought.message) == (
        True,
        f"subscribed to render node 1 for {NODE_SECONDS[0]} s",
    )
    assert (subscribed.ok, subscribed.message) == (True, "job 2: a piece of content ready in 6 s")
    assert game.world.residents[0].coins == 5_000 - on_demand_cost(
        CONTENT_WORKLOAD
    ) - subscription_cost(NODE_SECONDS[0])


def test_a_generative_node_cannot_render_and_a_missing_node_id_is_refused() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    tools.buy_node(NODE_SECONDS[0])

    wrong = tools.make_content("subscription", node_id=1)
    missing = tools.call("make_content", {"source": "subscription"})
    stray = tools.make_content("self", node_id=1)

    assert (wrong.code, missing.code, stray.code) == (
        "wrong_node_kind",
        "invalid_source",
        "invalid_source",
    )


def test_content_goes_into_the_slot_and_the_slot_is_paid_for_the_views_it_gets() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "a")

    placed = tools.place_content("L7-7")
    game.world = replace(
        game.world,
        crowd=Crowd(views=(("L7-7", 10.0),)),
        sponsors=SponsorBook((Sponsor(0, "s", 2.0, 1_000.0),)),
    )
    clock.now = PAY_PERIOD
    game.tick()

    assert (placed.ok, placed.message) == (True, "the ad slot on L7-7 now shows 1")
    assert game.world.residents[0].inventory.count(CONTENT_ITEM) == 1
    assert game.world.residents[0].coins == 5_000 + 20
    assert tools.status()["coins"] == 5_000 + 20


def test_placing_is_refused_for_a_foreign_bare_or_full_slot_or_without_content() -> None:
    game, _ = make_game()
    mine = ResidentTools(game, "a")
    theirs = ResidentTools(game, "b")

    foreign = theirs.place_content("L7-7")
    bare = mine.place_content("L1-1")
    none = theirs.place_content("L1-1")
    crowded = World(
        WORLD.map,
        (
            replace(WORLD.residents[0], inventory=Inventory(((CONTENT_ITEM, 1),))),
            WORLD.residents[1],
        ),
        parcels=(Parcel(7, 7, "a", Facility("ad", 0.0, (CONTENT_LIFETIME,) * AD_SLOT_CAPACITY)),),
    )
    full = ResidentTools(make_game(crowded)[0], "a").place_content("L7-7")

    assert (foreign.code, bare.code, none.code, full.code) == (
        "not_your_parcel",
        "no_content_facility",
        "not_your_parcel",
        "slot_full",
    )


def test_the_observation_shows_each_slots_content_and_income() -> None:
    game, clock = make_game()
    ResidentTools(game, "a").place_content("L7-7")
    game.world = replace(
        game.world,
        crowd=Crowd(views=(("L7-7", 10.0),)),
        sponsors=SponsorBook(last_paid=(("L7-7", 20),)),
    )
    clock.now = 100.0

    seen = look(game, "a")

    slot = next(parcel for parcel in seen.land.mine if parcel.id == "L7-7")
    assert slot.content_goes_stale_in == (CONTENT_LIFETIME - 100.0,)
    assert (slot.income_last_payday, slot.my_cut_last_payday, slot.people_seen) == (20, 20, 10.0)
    bare = next(parcel for parcel in seen.land.mine if parcel.id == "L1-1")
    assert (bare.content_goes_stale_in, bare.income_last_payday) == ((), 0)
    assert (seen.land.slot_capacity, seen.land.ad_content_lasts, seen.land.pay_period) == (
        AD_SLOT_CAPACITY,
        CONTENT_LIFETIME,
        PAY_PERIOD,
    )
    assert '"content":[{"source":"self"' in to_json(seen)


def test_the_observation_lists_content_jobs_without_a_parcel() -> None:
    game, _ = make_game()
    ResidentTools(game, "a").make_content("on_demand")

    seen = look(game, "a")

    job = seen.compute.my_jobs[0]
    assert (job.kind, job.parcel, job.ends_in) == ("make_content", None, 6.0)
    assert [(o.source, o.seconds) for o in seen.compute.content] == [
        ("self", 120.0),
        ("on_demand", 6.0),
        ("subscription", 6.0),
    ]


def test_content_is_offered_to_anyone_and_placing_to_those_with_content_and_room() -> None:
    game, _ = make_game()

    owner = selected_tool_names(look(game, "a"))
    stranger = selected_tool_names(look(game, "b"))

    assert {"make_content", "place_content", "buy_node"} <= owner
    assert "make_content" in stranger and "place_content" not in stranger


def test_placing_is_not_offered_without_content_or_with_every_slot_full() -> None:
    carrying = replace(WORLD.residents[0], inventory=Inventory())
    empty_handed = World(WORLD.map, (carrying, WORLD.residents[1]), parcels=WORLD.parcels)
    stocked = World(
        WORLD.map,
        WORLD.residents,
        parcels=(Parcel(7, 7, "a", Facility("ad", 0.0, (500.0,) * AD_SLOT_CAPACITY)),),
    )

    without = selected_tool_names(look(make_game(empty_handed)[0], "a"))
    full = selected_tool_names(look(make_game(stocked)[0], "a"))

    assert "place_content" not in without and "place_content" not in full


def test_the_last_request_offers_content_by_the_live_world() -> None:
    game, _ = make_game()

    owner = last_request_tool_names(ResidentTools(game, "a"))
    stranger = last_request_tool_names(ResidentTools(game, "b"))

    assert {"make_content", "place_content"} <= owner
    assert "make_content" in stranger and "place_content" not in stranger


def test_discovering_the_content_group_reveals_both_tools() -> None:
    names: set[str] = set()

    reply = discover_tools({"group": "content"}, names)

    assert names == set(TOOL_GROUPS["content"]) == {"make_content", "place_content"}
    assert '"ok":true' in reply


def test_tool_descriptions_quote_the_game_constants() -> None:
    descriptions = {tool.name: tool.description for tool in TOOLS}

    assert str(on_demand_cost(CONTENT_WORKLOAD)) in descriptions["make_content"]
    assert f"{AD_SLOT_CAPACITY} pieces" in descriptions["place_content"]
    assert f"{CONTENT_LIFETIME:g} s" in descriptions["place_content"]
