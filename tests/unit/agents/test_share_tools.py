"""Handing parcel shares to other residents through the agent tools, and what a model is told
about them."""

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.tool_selection import (
    TOOL_GROUPS,
    last_request_tool_names,
    selected_tool_names,
)
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import PARCEL_SHARES, Facility, Parcel
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.ads import CONTENT_ITEM, PAY_PER_FOOTFALL, PAY_PERIOD
from aiverse_server.economy.footfall import footfall
from aiverse_server.server.app import Game

# a bought L7-7, a slot in the middle of the 150 map that shows one piece of content, and L1-1,
# which is bare; b and c own nothing.
SLOT = Facility("ad", 0.0, (500.0,), PAY_PERIOD)
WORLD = World(
    map=WorldMap(size=150),
    residents=(
        Resident("a", "甲", "#112233", Position(75, 75), Inventory(((CONTENT_ITEM, 1),)), 5_000),
        Resident("b", "乙", "#445566", Position(20, 80), coins=5_000),
        Resident("c", "丙", "#778899", Position(30, 80), coins=5_000),
    ),
    parcels=(Parcel(7, 7, "a", SLOT), Parcel(1, 1, "a")),
)


class FakeClock:
    def __call__(self) -> float:
        return 0.0


def make_game(world: World = WORLD) -> Game:
    return Game(world, 600, FakeClock())


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def share_offer(tools: ResidentTools, to: str, shares: int, price: int) -> None:
    result = tools.call(
        "offer_shares", {"buyer_id": to, "parcel_id": "L7-7", "shares": shares, "price": price}
    )
    assert result.ok


def test_shares_are_offered_and_accepted_through_the_tools() -> None:
    game = make_game()
    share_offer(ResidentTools(game, "a"), "b", 30, 150)

    accepted = ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    parcel = game.world.parcels[0]
    assert accepted.ok
    assert parcel.shares == (("a", 70), ("b", 30))
    assert (game.world.residents[0].coins, game.world.residents[1].coins) == (5_150, 4_850)


def test_a_bad_offer_comes_back_as_a_failed_result_and_the_world_stays() -> None:
    game = make_game()
    tools = ResidentTools(game, "b")

    foreign = tools.call(
        "offer_shares", {"buyer_id": "c", "parcel_id": "L7-7", "shares": 5, "price": 1}
    )
    text = tools.call(
        "offer_shares", {"buyer_id": "c", "parcel_id": "L7-7", "shares": "5", "price": 1}
    )

    assert (foreign.ok, foreign.code) == (False, "not_enough_shares")
    assert (text.ok, text.code) == (False, "invalid_arguments")
    assert game.world.events.pending == ()


def test_the_observation_shows_who_holds_a_parcel_and_what_each_is_paid() -> None:
    game = make_game()
    share_offer(ResidentTools(game, "a"), "b", 40, 0)
    ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    mine = next(p for p in look(game, "a").land.mine if p.id == "L7-7")
    stake = look(game, "b").land.stakes[0]

    total = round(footfall(game.world, Position(75, 75)) * PAY_PER_FOOTFALL)
    assert mine.id == "L7-7" and mine.shares == {"a": 60, "b": 40}
    assert mine.income_per_payday == total
    assert mine.my_cut_per_payday + stake.my_cut_per_payday == total
    assert (stake.id, stake.owner, stake.my_shares) == ("L7-7", "a", 40)
    assert (stake.shares, stake.content, stake.income_per_payday) == (mine.shares, 1, total)
    assert look(game, "c").land.stakes == ()
    assert '"stakes":[{"id":"L7-7"' in to_json(look(game, "b"))


def test_a_pending_offer_shows_to_both_sides_with_what_the_shares_would_earn() -> None:
    game = make_game()
    share_offer(ResidentTools(game, "a"), "b", 50, 300)

    to_b = look(game, "b")
    from_a = look(game, "a")

    total = round(footfall(game.world, Position(75, 75)) * PAY_PER_FOOTFALL)
    (offer,) = to_b.share_offers_to_me
    assert (offer.event_id, offer.from_id, offer.to_id) == (1, "a", "b")
    assert (offer.parcel, offer.shares, offer.price) == ("L7-7", 50, 300)
    assert offer.cut_per_payday == total * 50 // PARCEL_SHARES
    assert from_a.my_share_offers == to_b.share_offers_to_me
    assert (to_b.my_share_offers, from_a.share_offers_to_me) == ((), ())
    assert (to_b.offers_to_me, to_b.information_offers_to_me) == ((), ())


def test_only_holders_are_offered_the_tool_and_the_answering_tools_follow_the_offers() -> None:
    game = make_game()
    share_offer(ResidentTools(game, "a"), "b", 50, 300)

    holder = selected_tool_names(look(game, "a"))
    recipient = selected_tool_names(look(game, "b"))
    bystander = selected_tool_names(look(game, "c"))

    assert {"offer_shares", "withdraw_offer"} <= holder and "respond_to_offer" not in holder
    assert "respond_to_offer" in recipient and "offer_shares" not in recipient
    assert not {"offer_shares", "respond_to_offer", "withdraw_offer"} & bystander


def test_a_partner_with_shares_but_no_parcel_can_pass_them_on() -> None:
    game = make_game()
    share_offer(ResidentTools(game, "a"), "b", 50, 0)
    ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    partner = selected_tool_names(look(game, "b"))
    last = last_request_tool_names(ResidentTools(game, "b"))

    assert "offer_shares" in partner and "offer_shares" in last
    assert "offer_shares" not in last_request_tool_names(ResidentTools(game, "c"))


def test_the_offers_group_reveals_the_share_tool() -> None:
    assert "offer_shares" in TOOL_GROUPS["offers"]


def test_the_owner_who_sold_everything_is_still_shown_the_parcel_as_theirs() -> None:
    game = make_game()
    share_offer(ResidentTools(game, "a"), "b", PARCEL_SHARES, 0)
    ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    mine = next(p for p in look(game, "a").land.mine if p.id == "L7-7")

    assert (mine.id, mine.shares, mine.my_cut_per_payday) == ("L7-7", {"b": 100}, 0)
