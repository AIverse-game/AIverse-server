from aiverse_server.agents.system_prompt import system_prompt
from aiverse_server.agents.tools import TOOLS
from aiverse_server.core.land import AD_SLOT_CAPACITY
from aiverse_server.core.world import NOTE_LENGTH_LIMIT
from aiverse_server.economy.admissions import FEES
from aiverse_server.economy.ads import CONTENT_LIFETIME, GAME_HALF_LIFE, GAME_ITEM
from aiverse_server.economy.compute import (
    AD_WORKLOAD,
    CONTENT_WORKLOAD,
    GAME_WORKLOAD,
    ON_DEMAND_PRICE,
    SUBSCRIPTION_PRICE,
    VENUE_WORKLOAD,
    node_supply,
)
from aiverse_server.economy.sponsors import PAY_PERIOD
from aiverse_server.economy.visitors import (
    GROWTH_FACILITIES,
    MAX_VISITORS,
    MIN_VIEW_SECONDS,
    ROUTE_SHARE,
    SEEN_FOR,
    VIEW_RADIUS,
    VISITOR_COUNT,
    visitors_for,
)
from aiverse_server.game.layout import create_default_world


def test_the_prompt_tells_a_model_that_thinking_time_is_game_time() -> None:
    prompt = system_prompt("甲", "a")

    assert "The game clock keeps running while you think" in prompt


def test_the_prompt_names_the_thinking_time_fields_of_the_observation() -> None:
    prompt = system_prompt("甲", "a")

    assert "last_thinking_seconds" in prompt and "avg_thinking_seconds" in prompt


def test_the_prompt_says_a_longer_note_is_refused() -> None:
    prompt = system_prompt("甲", "a")

    assert f"at most {NOTE_LENGTH_LIMIT} characters; a longer one is refused" in prompt


def test_the_prompt_says_only_coins_count_at_the_end() -> None:
    prompt = system_prompt("甲", "a")

    assert "Only coins count; land and items are worth nothing at the end." in prompt


def test_the_prompt_says_sponsors_pay_ad_slots_for_the_people_who_pass_them() -> None:
    prompt = system_prompt("甲", "a")

    assert "sponsors pay ad slots for the people who pass them" in prompt
    assert "A slot showing nothing counts no one and earns nothing" in " ".join(prompt.split())
    assert "buy_parcel" in prompt


def test_the_prompt_names_door_fees_as_a_second_source_of_coins() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert "Coins come into the world two ways" in prompt
    assert f"{FEES['workspace']} coins for a workspace and {FEES['game_hall']} for a game hall" in (
        prompt
    )


def test_the_prompt_says_nobody_goes_to_an_ad_and_only_passers_by_count() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert "Nobody goes to look at an ad" in prompt
    assert "and go to workspaces and game halls showing games" in prompt


def test_the_prompt_explains_venues_with_the_game_constants() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert f"{VENUE_WORKLOAD:g} workload" in prompt
    assert f"{ROUTE_SHARE:.0%} of the trips are a working route" in prompt
    assert f"make_content content=game, {GAME_WORKLOAD:g} workload" in prompt
    assert "A game hall draws nobody until it shows games" in prompt
    assert f"draws half as many visitors for every {GAME_HALF_LIFE:g}s" in prompt
    assert "a new game in a full hall pushes out the oldest" in prompt


def test_the_prompt_says_how_close_and_how_long_a_passer_by_must_be_to_count() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert f"spend {MIN_VIEW_SECONDS:g}s within {VIEW_RADIUS:g} units of it" in prompt
    assert f"counts again at a slot only after {SEEN_FOR:g}s" in prompt


def test_the_prompt_says_a_game_is_its_own_item_no_ad_slot_can_show() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert f"its own item, {GAME_ITEM!r}; an ad slot cannot show it" in prompt


def test_the_prompt_explains_content_with_the_game_constants() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert f"for {CONTENT_WORKLOAD:g} workload" in prompt
    assert f"each for {CONTENT_LIFETIME:g}s" in prompt
    assert f"Every {PAY_PERIOD:g}s the sponsors pay" in prompt
    assert f"up to {AD_SLOT_CAPACITY} pieces of content" in prompt
    assert "make_content" in prompt and "place_content" in prompt


def test_the_prompt_explains_compute_with_the_game_constants() -> None:
    prompt = system_prompt("甲", "a")

    assert f"{ON_DEMAND_PRICE} coins per workload" in prompt
    assert f"{SUBSCRIPTION_PRICE} coins/s" in prompt
    assert f"{AD_WORKLOAD:g} workload" in prompt
    assert "build_ad" in prompt and "buy_node" in prompt


def test_the_prompt_names_none_of_the_removed_mechanics() -> None:
    prompt = system_prompt("甲", "a").lower()

    removed = (
        "spring",
        "harvest",
        "collector",
        "program",
        "studio",
        "material",
        "market",
        "mine",
        "gather",
        "hosting",
        "meeting room",
    )
    assert not [word for word in removed if word in prompt]


def test_the_prompt_states_that_walking_costs_time_only() -> None:
    prompt = system_prompt("甲", "a")

    assert "straight line at 2.5 units/s and costs time only" in prompt


def test_the_move_tool_quotes_the_same_walking_speed_as_the_prompt() -> None:
    move = next(tool for tool in TOOLS if tool.name == "move_to")

    assert "costs time only" in move.description
    assert "2.5 map units per second" in move.description


def test_the_prompt_says_a_sell_order_is_listed_for_everyone_and_the_system_never_buys() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert "A resting sell order is posted on the shared book: every resident sees it" in prompt
    assert "the system never buys or sells" in prompt


def test_the_prompt_says_how_many_nodes_of_each_kind_the_town_has() -> None:
    supply = node_supply(create_default_world())

    prompt = " ".join(system_prompt("甲", "a").split())

    assert supply == 3
    assert f"the town has only {supply} generative and {supply} render nodes" in prompt
    assert "compute.town_nodes" in prompt


def test_the_prompt_says_the_visitors_grow_with_the_facilities_built() -> None:
    prompt = " ".join(system_prompt("甲", "a").split())

    assert (
        f"{VISITOR_COUNT} x sqrt(1 + facilities / {GROWTH_FACILITIES:g}), so {VISITOR_COUNT} with "
        f"none, {visitors_for(24)} with 24 and at most {MAX_VISITORS}"
    ) in prompt
    assert "land.visitors_drawn" in prompt
