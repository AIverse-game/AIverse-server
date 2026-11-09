"""Asking someone to build on a parcel, or for leave to, through the agent tools, and what a
model is told about it."""

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.system_prompt import system_prompt
from aiverse_server.agents.tool_selection import last_request_tool_names, selected_tool_names
from aiverse_server.agents.tools import TOOLS, ResidentTools
from aiverse_server.core.land import Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.compute import (
    AD_WORKLOAD,
    NODE_RATE,
    NODE_SECONDS,
    PERMIT_SECONDS,
    SELF_RATE,
)
from aiverse_server.server.app import Game

FEE = 300

# o owns L5-5 and stands on it; b owns nothing and has coins to spare; both are idle.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("o", "甲", "#112233", Position(55, 55), coins=5_000),
        Resident("b", "乙", "#445566", Position(20, 80), coins=5_000),
    ),
    parcels=(Parcel(5, 5, "o"),),
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


def ask_to_build(tools: ResidentTools, role: str = "owner", partner: str = "b") -> None:
    result = tools.call(
        "commission_build",
        {
            "partner_id": partner,
            "my_role": role,
            "parcel_id": "L5-5",
            "kind": "ad",
            "fee": FEE,
        },
    )
    assert result.ok, result


def test_a_commission_goes_through_the_tools_and_the_builder_is_paid_when_it_is_built() -> None:
    game, clock = make_game()
    ask_to_build(ResidentTools(game, "o"))
    builder = ResidentTools(game, "b")

    accepted = builder.call("respond_to_offer", {"event_id": 1, "accept": True})
    built = builder.build_ad("L5-5", "self")
    clock.now = AD_WORKLOAD / SELF_RATE
    paid = builder.call("remember", {"text": "built it", "term": "short_term"})

    assert accepted.ok
    assert (built.ok, built.message) == (
        True,
        f"job 1: ad slot on L5-5 ready in {AD_WORKLOAD / SELF_RATE:g} s",
    )
    assert paid.status is not None and paid.status["coins"] == 5_000 + FEE
    assert game.world.residents[0].coins == 5_000 - FEE


def test_a_builder_can_ask_the_owner_for_leave_through_the_tools() -> None:
    game, _ = make_game()
    ask_to_build(ResidentTools(game, "b"), role="builder", partner="o")

    accepted = ResidentTools(game, "o").call("respond_to_offer", {"event_id": 1, "accept": True})

    assert accepted.ok
    assert [(p.builder_id, p.owner_id, p.fee) for p in game.world.compute.permits] == [
        ("b", "o", FEE)
    ]


def test_bad_terms_come_back_as_failed_results_and_the_world_stays() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "o")

    other_parcel = tools.call(
        "commission_build",
        {"partner_id": "b", "my_role": "owner", "parcel_id": "L1-1", "kind": "ad", "fee": FEE},
    )
    wrong_role = tools.call(
        "commission_build",
        {"partner_id": "b", "my_role": "builder", "parcel_id": "L5-5", "kind": "ad", "fee": FEE},
    )
    portal = tools.call(
        "commission_build",
        {"partner_id": "b", "my_role": "owner", "parcel_id": "L5-5", "kind": "portal", "fee": 1},
    )

    assert not other_parcel.ok and not wrong_role.ok and not portal.ok
    assert (other_parcel.code, wrong_role.code, portal.code) == (
        "unknown_parcel",
        "not_your_parcel",
        "invalid_arguments",
    )
    assert game.world.residents[0].coins == 5_000
    assert game.world.events.pending == ()


def test_the_observation_shows_the_proposal_the_permit_and_the_building_to_both_sides() -> None:
    game, _ = make_game()
    ask_to_build(ResidentTools(game, "o"))

    asked = look(game, "b")
    mine = look(game, "o")
    ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})
    permitted = look(game, "b")
    ResidentTools(game, "b").build_ad("L5-5", "on_demand")
    building = look(game, "o")

    assert [
        (v.event_id, v.owner_id, v.builder_id, v.fee) for v in asked.build_commissions_to_me
    ] == [(1, "o", "b", FEE)]
    assert [v.event_id for v in mine.my_build_commissions] == [1]
    assert mine.me.coins_in_offers == FEE
    assert [(v.parcel, v.kind, v.fee) for v in permitted.build_permits] == [("L5-5", "ad", FEE)]
    assert permitted.build_permits[0].expires_in == PERMIT_SECONDS
    assert {p.id: p.building_ends_in for p in building.land.mine}["L5-5"] == (
        AD_WORKLOAD / NODE_RATE
    )


def test_a_builder_without_land_is_offered_the_build_tools_once_permitted() -> None:
    game, _ = make_game()
    ask_to_build(ResidentTools(game, "o"))
    before = selected_tool_names(look(game, "b"))
    ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    after = selected_tool_names(look(game, "b"))
    live = last_request_tool_names(ResidentTools(game, "b"))

    assert "build_ad" not in before and "respond_to_offer" in before
    assert {"build_ad", "commission_build"} <= after
    assert {"build_ad", "commission_build"} <= live
    assert "build_venue" not in after


def test_the_owner_is_offered_to_withdraw_a_commission_that_waits() -> None:
    game, _ = make_game()
    ask_to_build(ResidentTools(game, "o"))

    names = selected_tool_names(look(game, "o"))

    assert {"commission_build", "withdraw_offer"} <= names


def test_the_tool_and_the_prompt_explain_the_agreement_and_the_long_subscription() -> None:
    prompt = system_prompt("甲", "o")
    tool = next(spec for spec in TOOLS if spec.name == "commission_build")

    assert "commission_build" in prompt
    assert "must agree" in prompt
    assert f"{NODE_SECONDS[0]}-{NODE_SECONDS[1]}s" in prompt
    assert f"{PERMIT_SECONDS:g}" in tool.description


def test_a_promised_parcel_is_shown_as_promised_and_not_offered_to_its_owner_for_building() -> None:
    game, _ = make_game()
    ask_to_build(ResidentTools(game, "o"))
    ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})
    seen = look(game, "o")

    names = selected_tool_names(seen)
    live = last_request_tool_names(ResidentTools(game, "o"))

    assert [(p.id, p.promised_to) for p in seen.land.mine] == [("L5-5", "b")]
    assert "build_ad" not in names and "build_ad" not in live
