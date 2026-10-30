from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.core.events import accept_event, reject_event, send_event, withdraw_event
from aiverse_server.core.inventory import InventoryError, coin_balance, list_items
from aiverse_server.core.world import (
    Contract,
    ContractBook,
    Inventory,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.contracts import (
    MAX_CONTRACT_SECONDS,
    MAX_DELIVERIES,
    ContractError,
    ContractProposal,
    contract_coins,
    contracts_of,
)
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import (
    ActionRejected,
    CancelContract,
    Deliver,
    LeaveGame,
    ProposeContract,
    perform,
)
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.lifecycle import end_game, total_coins

# a has coins and wants tokens; b mines tokens for a.
WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident("a", "a", "#ffffff", Position(1, 1), Inventory(((TOKEN, 10),)), coins=2000),
        Resident("b", "b", "#000000", Position(9, 9), Inventory(((TOKEN, 100),)), coins=0),
    ),
)

# 30 tokens per delivery for 500 coins, up to 3 times within 60 seconds, proposed by the
# supplier or by the buyer.
BY_SUPPLIER = ContractProposal(True, TOKEN, 30, 500, 3, 60)
BY_BUYER = replace(BY_SUPPLIER, sender_supplies=False)


def holdings(world: World, resident_id: str) -> tuple[dict[str, int], int]:
    return list_items(world, resident_id), coin_balance(world, resident_id)


def signed(world: World = WORLD) -> World:
    """b proposes the terms and a accepts: contract 1 is in force from time 0."""
    world, proposal = send_event(world, "b", "a", BY_SUPPLIER)
    return accept_event(world, "a", proposal.id)


def the_contract(world: World) -> Contract:
    (contract,) = world.contracts.active
    return contract


# Signing.


def test_a_buyers_proposal_sets_aside_every_payment_at_once() -> None:
    world, proposal = send_event(WORLD, "a", "b", BY_BUYER)
    assert coin_balance(world, "a") == 500
    assert contract_coins(world, "a") == 1500
    world = accept_event(world, "b", proposal.id)
    contract = the_contract(world)
    assert (contract.supplier_id, contract.buyer_id) == ("b", "a")
    assert contract.escrow == 1500
    assert coin_balance(world, "a") == 500


def test_a_suppliers_proposal_takes_the_buyers_coins_on_acceptance() -> None:
    world, proposal = send_event(WORLD, "b", "a", BY_SUPPLIER)
    assert coin_balance(world, "a") == 2000
    assert contract_coins(world, "a") == 0
    world = accept_event(world, "a", proposal.id)
    assert coin_balance(world, "a") == 500
    assert the_contract(world).expires_at == 60


def test_a_buyer_short_of_coins_cannot_accept_and_the_proposal_stays() -> None:
    poor = replace(WORLD, residents=(replace(WORLD.residents[0], coins=1000), WORLD.residents[1]))
    world, proposal = send_event(poor, "b", "a", BY_SUPPLIER)
    with pytest.raises(InventoryError):
        accept_event(world, "a", proposal.id)
    assert world.events.pending == (proposal,)


@pytest.mark.parametrize("decline", [reject_event, withdraw_event])
def test_declining_a_buyers_proposal_hands_the_coins_back(decline: object) -> None:
    world, proposal = send_event(WORLD, "a", "b", BY_BUYER)
    answerer = "b" if decline is reject_event else "a"
    world = decline(world, answerer, proposal.id)  # type: ignore[operator]
    assert coin_balance(world, "a") == 2000
    assert world.contracts.active == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"sender_supplies": 1},
        {"deliveries": 0},
        {"deliveries": MAX_DELIVERIES + 1},
        {"duration": 0},
        {"duration": MAX_CONTRACT_SECONDS + 1},
        {"price": 0},
        {"quantity": True},
        {"kind": ""},
        {"price": 10**9, "deliveries": 2},
    ],
)
def test_unworkable_terms_are_refused(changes: dict[str, Any]) -> None:
    proposal = replace(BY_SUPPLIER, **changes)
    with pytest.raises(ContractError) as refusal:
        send_event(WORLD, "b", "a", proposal)
    assert refusal.value.code == "invalid_contract"


# Delivering.


def test_each_delivery_moves_the_items_and_pays_from_the_coins_set_aside() -> None:
    world = perform(signed(), Deliver("b", 1))
    assert holdings(world, "b") == ({TOKEN: 70}, 500)
    assert holdings(world, "a") == ({TOKEN: 40}, 500)
    assert the_contract(world).deliveries_left == 2
    assert the_contract(world).escrow == 1000


def test_the_last_delivery_ends_the_contract() -> None:
    world = signed()
    for _ in range(3):
        world = perform(world, Deliver("b", 1))
    assert holdings(world, "b") == ({TOKEN: 10}, 1500)
    assert holdings(world, "a") == ({TOKEN: 100}, 500)
    assert world.contracts.active == ()
    with pytest.raises(ActionRejected) as rejection:
        perform(world, Deliver("b", 1))
    assert rejection.value.code == "unknown_contract"


def test_a_supplier_without_the_items_cannot_deliver() -> None:
    world = signed()
    short = replace(
        world,
        residents=(
            world.residents[0],
            replace(world.residents[1], inventory=Inventory(((TOKEN, 29),))),
        ),
    )
    with pytest.raises(ActionRejected) as rejection:
        perform(short, Deliver("b", 1))
    assert rejection.value.code == "not_enough_items"


def test_only_the_supplier_delivers_or_cancels() -> None:
    world = signed()
    for action in (Deliver("a", 1), CancelContract("a", 1)):
        with pytest.raises(ActionRejected) as rejection:
            perform(world, action)
        assert rejection.value.code == "not_supplier"


def test_outsiders_see_no_contract() -> None:
    world = World(
        map=WORLD.map,
        residents=(*WORLD.residents, Resident("c", "c", "#123456", Position(5, 5))),
    )
    world = signed(world)
    with pytest.raises(ActionRejected) as rejection:
        perform(world, Deliver("c", 1))
    assert rejection.value.code == "unknown_contract"
    assert contracts_of(world, "c") == ()


def test_a_delivery_of_the_last_token_takes_the_supplier_out() -> None:
    world = signed()
    world = replace(
        world,
        residents=(
            world.residents[0],
            replace(world.residents[1], inventory=Inventory(((TOKEN, 30),))),
        ),
    )
    world = perform(world, Deliver("b", 1))
    supplier = world.residents[1]
    assert supplier.left
    assert supplier.coins == 500
    # Leaving ended the contract and gave the buyer the rest back.
    assert world.contracts.active == ()
    assert coin_balance(world, "a") == 1500


# Ending.


def test_a_contract_lapses_on_time_and_the_buyer_gets_the_rest_back() -> None:
    world = perform(signed(), Deliver("b", 1))
    assert next_due(world) == 60
    assert settle_due(replace(world, time=59.9)).contracts.active != ()
    world = settle_due(replace(world, time=60))
    assert world.contracts.active == ()
    assert coin_balance(world, "a") == 1500
    assert next_due(world) is None


def test_a_lapsed_contract_takes_no_more_deliveries_even_before_the_clock_settles() -> None:
    world = replace(signed(), time=60)
    with pytest.raises(ActionRejected) as rejection:
        perform(world, Deliver("b", 1))
    assert rejection.value.code == "unknown_contract"


def test_the_supplier_may_cancel_and_the_buyer_gets_the_rest_back() -> None:
    world = perform(signed(), Deliver("b", 1))
    world = perform(world, CancelContract("b", 1))
    assert world.contracts.active == ()
    assert holdings(world, "a") == ({TOKEN: 40}, 1500)
    assert holdings(world, "b") == ({TOKEN: 70}, 500)


@pytest.mark.parametrize("leaver", ["a", "b"])
def test_either_party_leaving_ends_the_contract(leaver: str) -> None:
    world = perform(signed(), LeaveGame(leaver))
    assert world.contracts.active == ()
    assert coin_balance(world, "a") == 2000


def test_the_game_ending_ends_every_contract_and_proposal() -> None:
    world, _ = send_event(signed(), "a", "b", replace(BY_BUYER, price=100))
    assert coin_balance(world, "a") == 200
    world = end_game(world)
    assert world.contracts.active == ()
    assert world.events.pending == ()
    assert coin_balance(world, "a") == 2000


def test_coins_set_aside_still_count_for_the_buyer() -> None:
    world, _ = send_event(signed(), "a", "b", replace(BY_BUYER, price=100))
    assert coin_balance(world, "a") == 200
    assert total_coins(world, "a") == 2000


# Through primitives.


def test_propose_contract_sends_a_proposal_either_side_may_make() -> None:
    world = perform(WORLD, ProposeContract("a", "b", False, TOKEN, 30, 500, 3, 60))
    (event,) = world.events.pending
    assert event.payload == BY_BUYER
    assert coin_balance(world, "a") == 500
    with pytest.raises(ActionRejected) as rejection:
        perform(WORLD, ProposeContract("a", "a", True, TOKEN, 30, 500, 3, 60))
    assert rejection.value.code == "self_addressed"
    with pytest.raises(ActionRejected) as rejection:
        perform(WORLD, ProposeContract("a", "b", False, TOKEN, 30, 1000, 3, 60))
    assert rejection.value.code == "not_enough_coins"


def test_the_world_refuses_contracts_that_cannot_be() -> None:
    contract = the_contract(signed())
    with pytest.raises(ValueError):
        replace(signed(), ended=True)
    with pytest.raises(ValueError):
        ContractBook((replace(contract, deliveries_left=0),), 2)
    with pytest.raises(ValueError):
        ContractBook((replace(contract, expires_at=contract.signed_at),), 2)
    with pytest.raises(ValueError):
        replace(WORLD, contracts=ContractBook((replace(contract, buyer_id="b"),), 2))
