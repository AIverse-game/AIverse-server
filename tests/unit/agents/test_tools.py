import json
from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tools import TOOLS, ResidentTools, ToolSpec, tool_specs
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.server.app import Game

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((TOKEN, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((TOKEN, 100),)), coins=5000),
    ),
    pools=(Pool("p1", Position(80, 50), 4, 300),),
    market=Market(Position(50, 50), 5),
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

    result = tools.call("buy_chip", {"model": "MK1"})

    assert result.ok
    assert me(game, "a").inventory.count("MK1") == 1
    assert me(game, "b").inventory.count("MK1") == 0
    assert result.status is not None and result.status["coins"] == 4700


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
        ("sell_tokens", '{"quantity": ' + "9" * 5000 + "}", "invalid_arguments"),
        ("sell_tokens", {"quantity": "5"}, "invalid_arguments"),
        ("move_to", {"x": 500, "y": 2}, "out_of_bounds"),
        ("mine", {"pool_id": "p1"}, "no_chip"),
        ("buy_chip", {"model": "MK9"}, "unknown_chip"),
        ("forget", {"note_id": 3}, "unknown_note"),
        ("sell_tokens", {"quantity": 100}, "last_token"),
        (
            "offer_to_sell",
            {"buyer_id": "b", "kind": "token", "quantity": 100, "price": 1},
            "last_token",
        ),
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


def test_a_walk_that_would_spend_the_last_token_is_refused() -> None:
    poor = replace(WORLD.residents[0], inventory=Inventory(((TOKEN, 10),)))
    game, _ = make_game(replace(WORLD, residents=(poor, WORLD.residents[1])))
    before = game.world

    # From (50, 50) to (60, 50) costs 10 tokens, all the resident has.
    result = ResidentTools(game, "a").call("move_to", {"x": 60, "y": 50})

    assert result.code == "last_token"
    assert game.world == before


def test_a_whisper_on_the_way_that_would_spend_the_last_token_is_refused() -> None:
    poor = replace(WORLD.residents[0], inventory=Inventory(((TOKEN, 11),)))
    game, _ = make_game(replace(WORLD, residents=(poor, WORLD.residents[1])))
    tools = ResidentTools(game, "a")
    assert tools.call("move_to", {"x": 60, "y": 50}).ok

    result = tools.call("say", {"text": "hi", "to": "b"})

    assert result.code == "last_token"
    assert me(game, "a").inventory.count(TOKEN) == 1


def test_selling_all_but_one_token_keeps_the_resident_in_the_game() -> None:
    game, _ = make_game()

    result = ResidentTools(game, "a").call("sell_tokens", {"quantity": 99})

    assert result.ok
    assert me(game, "a").inventory.count(TOKEN) == 1
    assert not me(game, "a").left


@pytest.mark.parametrize("parts", [[30], [10, 20], [1] * 30])
def test_splitting_a_sale_within_a_round_earns_no_more_than_selling_at_once(
    parts: list[int],
) -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a", seen_pressure=0.0)

    for part in parts:
        assert tools.call("sell_tokens", {"quantity": part}).ok

    # 30 tokens sold at once into an untouched market fetch 578 coins, see the market's tests;
    # parts round down on their own.
    assert 5000 + 578 - len(parts) < me(game, "a").coins <= 5000 + 578


def test_tools_take_arguments_as_json_text_as_models_send_them() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    moved = tools.call("move_to", '{"x": 60, "y": 50}')
    remembered = tools.call("remember", '{"text": "pool p1 east", "term": "long_term"}')

    assert moved.ok and remembered.ok
    assert tools.calls[0].arguments == {"x": 60, "y": 50}
    assert [note.text for note in me(game, "a").memory.long_term] == ["pool p1 east"]


def test_the_forget_tool_drops_a_note_the_resident_remembered() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    assert tools.call("remember", {"text": "pool p1 east", "term": "long_term"}).ok
    note = me(game, "a").memory.long_term[0]

    forgot = tools.call("forget", {"note_id": note.id})

    assert forgot.ok
    assert me(game, "a").memory.long_term == ()


def test_trading_tools_send_and_answer_offers() -> None:
    game, _ = make_game()
    seller, buyer = ResidentTools(game, "a"), ResidentTools(game, "b")

    offered = seller.call(
        "offer_to_sell", {"buyer_id": "b", "kind": "token", "quantity": 10, "price": 150}
    )
    event_id = game.world.events.pending[0].id
    accepted = buyer.call("respond_to_offer", {"event_id": event_id, "accept": True})

    assert offered.ok and accepted.ok
    assert me(game, "a").coins == 5150
    assert me(game, "b").inventory.count(TOKEN) == 110


def proposed() -> tuple[Game, FakeClock]:
    """a, buying, proposes b 2 deliveries of 20 tokens for 300 coins within 120 s, b staking
    50 on each."""
    game, clock = make_game()
    assert ResidentTools(game, "a").propose_contract("b", "buyer", TOKEN, 20, 300, 2, 120, 50).ok
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

    (mine,) = supplier.my_contracts
    (theirs,) = buyer.my_contracts
    assert (theirs.contract_id, theirs.my_role, theirs.partner_id) == (
        mine.contract_id,
        "buyer",
        "b",
    )
    assert supplier.me.coins_in_contracts == 100
    assert (mine.my_role, mine.partner_id, mine.escrow, mine.deposit_held, mine.expires_in) == (
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
    assert me(game, "a").inventory.count(TOKEN) == 120


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
