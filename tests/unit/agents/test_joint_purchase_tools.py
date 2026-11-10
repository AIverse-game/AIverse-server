"""Buying a parcel together through the agent tools, and what a model is told about it."""

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tool_selection import (
    TOOL_GROUPS,
    last_request_tool_names,
    selected_tool_names,
)
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.joint_purchase import JOINT_PURCHASE_SECONDS
from aiverse_server.economy.visitors import people_near
from aiverse_server.server.app import Game

# A 10 x 10 grid of parcels, L5-3 costing 2540. a stands in it, b is far away, c is walking.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(55, 35), coins=5_000),
        Resident("b", "乙", "#445566", Position(5, 5), coins=5_000),
        Resident("c", "丙", "#778899", Position(56, 36), coins=5_000),
    ),
)


class FakeClock:
    def __call__(self) -> float:
        return 0.0


def make_game(world: World = WORLD) -> Game:
    return Game(world, 600, FakeClock())


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def propose(tools: ResidentTools, partner: str = "b") -> None:
    result = tools.call(
        "joint_buy_parcel", {"partner_id": partner, "partner_pays": 1_000, "partner_shares": 40}
    )
    assert result.ok


def test_a_parcel_is_bought_together_through_the_tools() -> None:
    game = make_game()
    propose(ResidentTools(game, "a"))

    accepted = ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    assert accepted.ok
    assert game.world.parcels == (Parcel(5, 3, "a", shares=(("a", 60), ("b", 40))),)


def test_bad_terms_come_back_as_failed_results_and_the_world_stays() -> None:
    game = make_game()
    tools = ResidentTools(game, "a")

    text = tools.call(
        "joint_buy_parcel", {"partner_id": "b", "partner_pays": "1000", "partner_shares": 40}
    )
    nothing = tools.call(
        "joint_buy_parcel", {"partner_id": "b", "partner_pays": 100, "partner_shares": 0}
    )
    assert (text.code, nothing.code) == ("invalid_arguments", "invalid_shares")
    assert game.world.events.pending == ()


def test_the_observation_shows_the_proposal_to_both_and_locks_no_coins() -> None:
    game = make_game()
    propose(ResidentTools(game, "a"))

    from_a = look(game, "a")
    to_b = look(game, "b")

    (offer,) = to_b.joint_purchases_to_me
    assert (offer.event_id, offer.from_id, offer.to_id, offer.parcel) == (1, "a", "b", "L5-3")
    assert (offer.price, offer.from_pays, offer.to_pays, offer.to_shares) == (
        2_540,
        1_540,
        1_000,
        40,
    )
    assert offer.people_near == people_near(game.world, offer.centre)
    assert from_a.my_joint_purchases == to_b.joint_purchases_to_me
    assert (to_b.my_joint_purchases, from_a.joint_purchases_to_me) == ((), ())
    assert offer.expires_in == JOINT_PURCHASE_SECONDS
    assert from_a.me.coins == 5_000 and from_a.me.coins_in_offers == 0
    assert [r.coins for r in from_a.residents if r.id == "a"] == [5_000]


def test_the_observation_says_when_the_parcel_was_bought_by_someone_else() -> None:
    game = make_game()
    propose(ResidentTools(game, "a"))
    before = look(game, "b").joint_purchases_to_me[0].parcel_taken
    ResidentTools(game, "c").call("buy_parcel", {})

    after = look(game, "b").joint_purchases_to_me[0].parcel_taken

    assert (before, after) == (False, True)


def test_the_tool_is_offered_in_a_free_parcel_and_the_answering_tools_follow_the_offers() -> None:
    game = make_game()
    propose(ResidentTools(game, "a"))

    in_free = selected_tool_names(look(game, "c"))
    proposer = selected_tool_names(look(game, "a"))
    partner = selected_tool_names(look(game, "b"))

    assert "joint_buy_parcel" in in_free and "joint_buy_parcel" in proposer
    assert "withdraw_offer" in proposer and "respond_to_offer" not in proposer
    assert "respond_to_offer" in partner


def test_the_tool_is_not_offered_in_a_parcel_somebody_owns_and_the_group_reveals_it() -> None:
    game = make_game(World(WORLD.map, WORLD.residents, parcels=(Parcel(5, 3, "b"),)))

    seen = selected_tool_names(look(game, "a"))
    last = last_request_tool_names(ResidentTools(game, "a"))

    assert "joint_buy_parcel" not in seen and "joint_buy_parcel" not in last
    assert "joint_buy_parcel" in TOOL_GROUPS["land"]
    assert "joint_buy_parcel" in last_request_tool_names(ResidentTools(make_game(), "a"))
