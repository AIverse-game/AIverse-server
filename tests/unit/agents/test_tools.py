import json
from dataclasses import asdict
from typing import Any

import pytest

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tools import TOOLS, ResidentTools, ToolSpec, tool_specs
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.server.app import Game

POSTER = "poster"
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((POSTER, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((POSTER, 100),)), coins=5000),
    ),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    # The turn first: it brings the world up to now.
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def me(game: Game, resident_id: str) -> Resident:
    return next(r for r in game.world.residents if r.id == resident_id)


def test_tools_act_for_their_own_resident_only() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    result = tools.call("buy_parcel", {})

    assert result.ok
    assert [parcel.owner_id for parcel in game.world.parcels] == ["a"]
    assert result.status is not None and result.status["coins"] == 5000 - 2790


@pytest.mark.parametrize("spec", TOOLS, ids=lambda spec: spec.name)
def test_no_tool_takes_the_resident_who_acts(spec: ToolSpec) -> None:
    assert "resident_id" not in spec.args.model_fields


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("leave_game", {}, "unknown_tool"),
        ("move_to", {"x": 1}, "invalid_arguments"),
        ("move_to", {"x": True, "y": 2}, "invalid_arguments"),
        ("move_to", {"x": float("nan"), "y": 2}, "invalid_arguments"),
        ("move_to", '{"x": 1, "y": 2, "resident_id": "b"}', "invalid_arguments"),
        ("move_to", "not json", "invalid_arguments"),
        ("move_to", "[1, 2]", "invalid_arguments"),
        ("move_to", {"x": 500, "y": 2}, "out_of_bounds"),
        ("forget", {"note_id": 3}, "unknown_note"),
        ("buy_parcel", {"x": 1}, "invalid_arguments"),
        ("place_order", {"side": "buy", "item": POSTER, "quantity": 1, "price": 1}, "unknown_item"),
        ("harvest", {"spring_id": "p1"}, "unknown_tool"),
    ],
)
def test_bad_calls_come_back_as_failures_and_change_nothing(
    name: str, arguments: dict[str, Any] | str, code: str
) -> None:
    game, _ = make_game()
    before = game.world
    tools = ResidentTools(game, "a")

    result = tools.call(name, arguments)

    assert not result.ok
    assert result.code == code
    assert game.world == before
    assert len(tools.calls) == 1
    assert json.loads(result.to_json())["ok"] is False


def test_a_walk_costs_time_and_nothing_else() -> None:
    game, _ = make_game()

    result = ResidentTools(game, "a").call("move_to", {"x": 70, "y": 50})

    assert result.ok and result.status is not None
    assert result.status["walking_to"] == {"x": 70.0, "y": 50.0}
    assert result.status["arrives_in"] == 8.0
    assert (result.status["coins"], result.status["items"]) == (5000, {POSTER: 100})


def test_a_resident_cannot_act_while_walking_but_can_still_whisper() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    assert tools.call("move_to", {"x": 70, "y": 50}).ok

    parcel = tools.call("buy_parcel", {})
    whisper = tools.call("say", {"text": "hi", "to": "b"})

    assert (parcel.ok, parcel.code) == (False, "busy")
    assert whisper.ok
    assert me(game, "a").inventory.count(POSTER) == 100


def test_tools_take_arguments_as_json_text_as_models_send_them() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    moved = tools.call("move_to", '{"x": 60, "y": 50}')
    remembered = tools.call("remember", '{"text": "parcel L1-1 is cheap", "term": "long_term"}')

    assert moved.ok and remembered.ok
    assert tools.calls[0].arguments == {"x": 60, "y": 50}
    assert [note.text for note in me(game, "a").memory.long_term] == ["parcel L1-1 is cheap"]


def test_the_remember_tool_keeps_a_note_short_term_when_no_term_is_given() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    remembered = tools.call("remember", {"text": "parcel L1-1 is cheap"})

    assert remembered.ok
    assert [note.text for note in me(game, "a").memory.short_term] == ["parcel L1-1 is cheap"]
    assert me(game, "a").memory.long_term == ()


def test_the_forget_tool_drops_a_note_the_resident_remembered() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    assert tools.call("remember", {"text": "parcel L1-1 is cheap", "term": "long_term"}).ok
    note = me(game, "a").memory.long_term[0]

    forgot = tools.call("forget", {"note_id": note.id})

    assert forgot.ok
    assert me(game, "a").memory.long_term == ()


def test_trading_tools_send_and_answer_offers() -> None:
    game, _ = make_game()
    seller, buyer = ResidentTools(game, "a"), ResidentTools(game, "b")

    offered = seller.call(
        "offer_to_sell", {"buyer_id": "b", "kind": POSTER, "quantity": 10, "price": 150}
    )
    event_id = game.world.events.pending[0].id
    accepted = buyer.call("respond_to_offer", {"event_id": event_id, "accept": True})

    assert offered.ok and accepted.ok
    assert me(game, "a").coins == 5150
    assert me(game, "b").inventory.count(POSTER) == 110


def test_selling_information_shows_the_buyer_only_the_summary_until_they_pay() -> None:
    game, _ = make_game()
    seller, buyer = ResidentTools(game, "a"), ResidentTools(game, "b")
    arguments = {
        "buyer_id": "b",
        "summary": "a cheap parcel",
        "content": "the plot Zeta-9 near (95, 95) is cheap and busy",
        "price": 150,
    }

    offered = seller.call("offer_information", arguments)
    before = look(game, "b")
    event_id = game.world.events.pending[0].id
    accepted = buyer.call("respond_to_offer", {"event_id": event_id, "accept": True})

    assert offered.ok and accepted.ok
    [offer] = before.information_offers_to_me
    assert (offer.from_id, offer.summary, offer.price) == ("a", "a cheap parcel", 150)
    assert "Zeta-9" not in json.dumps(asdict(before))
    assert me(game, "a").coins == 5150 and me(game, "b").coins == 4850
    assert "Zeta-9" in me(game, "b").memory.long_term[0].text
    assert me(game, "a").memory == WORLD.residents[0].memory


def test_the_seller_sees_their_own_information_offer_without_the_content() -> None:
    game, _ = make_game()
    ResidentTools(game, "a").call(
        "offer_information",
        {
            "buyer_id": "b",
            "summary": "a cheap parcel",
            "content": "the plot Zeta-9 is cheap",
            "price": 150,
        },
    )

    seen = look(game, "a")

    [offer] = seen.my_information_offers
    assert (offer.to_id, offer.summary, offer.price) == ("b", "a cheap parcel", 150)
    assert "Zeta-9" not in json.dumps(asdict(seen))


@pytest.mark.parametrize(
    "arguments",
    [
        {"buyer_id": "b", "summary": "s", "content": "c", "price": True},
        {"buyer_id": "b", "summary": "s", "content": "c", "price": 1.5},
        {"buyer_id": "b", "summary": "s", "content": "c"},
        {"buyer_id": "b", "summary": "s", "price": 5},
    ],
)
def test_information_offers_with_bad_arguments_fail_and_change_nothing(
    arguments: dict[str, Any],
) -> None:
    game, _ = make_game()
    before = game.world

    result = ResidentTools(game, "a").call("offer_information", arguments)

    assert not result.ok and result.code == "invalid_arguments"
    assert game.world == before


def proposed() -> tuple[Game, FakeClock]:
    """a, buying, proposes b 2 deliveries of 20 posters for 300 coins within 120 s, b staking
    50 on each."""
    game, clock = make_game()
    assert ResidentTools(game, "a").propose_contract("b", "buyer", POSTER, 20, 300, 2, 120, 50).ok
    return game, clock


def signed() -> tuple[Game, FakeClock, int]:
    """The proposal accepted at time 0; returns the contract id too."""
    game, clock = proposed()
    proposal = look(game, "b").contract_proposals_to_me[0]
    accepted = ResidentTools(game, "b").call(
        "respond_to_offer", {"event_id": proposal.event_id, "accept": True}
    )
    assert accepted.ok
    (contract,) = look(game, "b").my_contracts
    return game, clock, contract.contract_id


def test_the_propose_tool_sends_the_terms_and_sets_the_payments_aside() -> None:
    game, _ = proposed()

    proposal = look(game, "b").contract_proposals_to_me[0]
    buyer = look(game, "a").me

    assert (proposal.supplier_id, proposal.buyer_id, proposal.deliveries, proposal.deposit) == (
        "b",
        "a",
        2,
        50,
    )
    assert buyer.coins_in_contracts == 600


def test_accepting_a_proposal_signs_the_contract_for_both_parties() -> None:
    game, clock, _ = signed()

    clock.now = 30
    supplier = look(game, "b")
    buyer = look(game, "a")

    (own,) = supplier.my_contracts
    (theirs,) = buyer.my_contracts
    assert (theirs.contract_id, theirs.my_role, theirs.partner_id) == (
        own.contract_id,
        "buyer",
        "b",
    )
    assert supplier.me.coins_in_contracts == 100
    assert (
        own.my_role,
        own.partner_id,
        own.escrow,
        own.deposit_held,
        own.expires_in,
    ) == (
        "supplier",
        "a",
        600,
        100,
        90,
    )


def test_the_deliver_tool_works_for_the_supplier_only() -> None:
    game, _, contract_id = signed()

    refused = ResidentTools(game, "a").deliver(contract_id)
    delivered = ResidentTools(game, "b").deliver(contract_id)

    assert not refused.ok and refused.code == "not_supplier"
    assert delivered.ok
    assert me(game, "b").coins == 5000 - 100 + 300 + 50
    assert me(game, "a").inventory.count(POSTER) == 120


def test_cancelling_shows_the_buyer_how_the_contract_ended() -> None:
    game, _, contract_id = signed()
    supplier = ResidentTools(game, "b")
    assert supplier.deliver(contract_id).ok

    cancelled = supplier.call("cancel_contract", {"contract_id": contract_id})
    seen = look(game, "a")

    assert cancelled.ok
    # The payment left and the deposit for the delivery not made.
    assert me(game, "a").coins == 5000 - 600 + 300 + 50
    assert seen.my_contracts == ()
    (ended,) = seen.ended_contracts
    assert (ended.reason, ended.my_role, ended.deliveries_missed, ended.deposit_forfeited) == (
        "cancelled",
        "buyer",
        1,
        50,
    )


def test_tool_specs_are_function_schemas() -> None:
    specs = tool_specs()
    assert {spec["function"]["name"] for spec in specs} == {spec.name for spec in TOOLS}
    move = next(spec for spec in specs if spec["function"]["name"] == "move_to")
    assert move["type"] == "function"
    assert set(move["function"]["parameters"]["required"]) == {"x", "y"}


def test_tool_specs_leave_out_titles_that_only_repeat_names() -> None:
    specs = tool_specs()

    assert '"title"' not in json.dumps(specs)
    move = next(spec for spec in specs if spec["function"]["name"] == "move_to")
    assert set(move["function"]["parameters"]["properties"]) == {"x", "y"}
    assert move["function"]["parameters"]["properties"]["x"]["type"] == "number"
