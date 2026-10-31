import json
from typing import Any

import pytest

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tools import TOOLS, ResidentTools, tool_specs
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
    # No tool takes the resident who acts.
    for spec in TOOLS:
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


def test_tools_take_arguments_as_json_text_as_models_send_them() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    assert tools.call("move_to", '{"x": 60, "y": 50}').ok
    assert tools.calls[0].arguments == {"x": 60, "y": 50}
    assert tools.call("remember", '{"text": "pool p1 east", "term": "long_term"}').ok
    note = me(game, "a").memory.long_term[0]
    assert tools.call("forget", {"note_id": note.id}).ok
    assert me(game, "a").memory.long_term == ()


def test_trading_tools_send_and_answer_offers() -> None:
    game, _ = make_game()
    seller = ResidentTools(game, "a")
    assert seller.call(
        "offer_to_sell", {"buyer_id": "b", "kind": "token", "quantity": 10, "price": 150}
    ).ok
    event_id = game.world.events.pending[0].id
    assert (
        ResidentTools(game, "b").call("respond_to_offer", {"event_id": event_id, "accept": True}).ok
    )
    assert me(game, "a").coins == 5150
    assert me(game, "b").inventory.count(TOKEN) == 110


def test_contract_tools_propose_sign_deliver_and_cancel() -> None:
    game, clock = make_game()
    buyer, supplier = ResidentTools(game, "a"), ResidentTools(game, "b")
    assert buyer.propose_contract("b", "buyer", TOKEN, 20, 300, 2, 120, deposit=50).ok
    proposal = look(game, "b").contract_proposals_to_me[0]
    assert (proposal.supplier_id, proposal.buyer_id, proposal.deliveries, proposal.deposit) == (
        "b",
        "a",
        2,
        50,
    )
    assert look(game, "a").me.coins_in_contracts == 600
    assert supplier.call("respond_to_offer", {"event_id": proposal.event_id, "accept": True}).ok
    assert look(game, "b").me.coins_in_contracts == 100
    clock.now = 30
    (mine,) = look(game, "b").my_contracts
    assert (mine.my_role, mine.partner_id, mine.escrow, mine.deposit_held, mine.expires_in) == (
        "supplier",
        "a",
        600,
        100,
        90,
    )
    refused = buyer.deliver(mine.contract_id)
    assert not refused.ok and refused.code == "not_supplier"
    assert supplier.deliver(mine.contract_id).ok
    assert me(game, "b").coins == 5000 - 100 + 300 + 50
    assert me(game, "a").inventory.count(TOKEN) == 120
    assert supplier.call("cancel_contract", {"contract_id": mine.contract_id}).ok
    # The payment left and the deposit for the delivery not made.
    assert me(game, "a").coins == 5000 - 600 + 300 + 50
    seen = look(game, "a")
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
