from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.core.events import accept_event, reject_event, send_event, withdraw_event
from aiverse_server.core.inventory import InventoryError, coin_balance, list_items
from aiverse_server.core.world import (
    CONTRACT_END_LIMIT,
    Contract,
    ContractBook,
    ContractEnd,
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
from aiverse_server.economy.energy import ENERGY
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

# a has coins and wants energy; b harvests energy for a.
WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident("a", "a", "#ffffff", Position(1, 1), Inventory(((ENERGY, 10),)), coins=2000),
        Resident("b", "b", "#000000", Position(9, 9), Inventory(((ENERGY, 100),)), coins=0),
    ),
)

# 30 energy per delivery for 500 coins, up to 3 times within 60 seconds, proposed by the
# supplier or by the buyer.
BY_SUPPLIER = ContractProposal(True, ENERGY, 30, 500, 3, 60)
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
    proposed, proposal = send_event(WORLD, "a", "b", BY_BUYER)
    signed_world = accept_event(proposed, "b", proposal.id)

    assert coin_balance(proposed, "a") == 500
    assert contract_coins(proposed, "a") == 1500
    contract = the_contract(signed_world)
    assert (contract.supplier_id, contract.buyer_id) == ("b", "a")
    assert contract.escrow == 1500
    assert coin_balance(signed_world, "a") == 500


def test_a_suppliers_proposal_takes_the_buyers_coins_on_acceptance() -> None:
    proposed, proposal = send_event(WORLD, "b", "a", BY_SUPPLIER)
    signed_world = accept_event(proposed, "a", proposal.id)

    assert coin_balance(proposed, "a") == 2000
    assert contract_coins(proposed, "a") == 0
    assert coin_balance(signed_world, "a") == 500
    assert the_contract(signed_world).expires_at == 60


def test_a_buyer_short_of_coins_cannot_accept_and_the_proposal_stays() -> None:
    poor = replace(WORLD, residents=(replace(WORLD.residents[0], coins=1000), WORLD.residents[1]))
    world, proposal = send_event(poor, "b", "a", BY_SUPPLIER)
    with pytest.raises(InventoryError):
        accept_event(world, "a", proposal.id)
    assert world.events.pending == (proposal,)


@pytest.mark.parametrize(("decline", "answerer"), [(reject_event, "b"), (withdraw_event, "a")])
def test_declining_a_buyers_proposal_hands_the_coins_back(
    decline: Callable[[World, str, int], World], answerer: str
) -> None:
    world, proposal = send_event(WORLD, "a", "b", BY_BUYER)

    world = decline(world, answerer, proposal.id)

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
        {"deposit": -1},
        {"deposit": True},
        {"deposit": 10**9, "deliveries": 2},
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
    assert holdings(world, "b") == ({ENERGY: 70}, 500)
    assert holdings(world, "a") == ({ENERGY: 40}, 500)
    assert the_contract(world).deliveries_left == 2
    assert the_contract(world).escrow == 1000


def test_the_last_delivery_ends_the_contract() -> None:
    world = signed()
    for _ in range(3):
        world = perform(world, Deliver("b", 1))
    assert holdings(world, "b") == ({ENERGY: 10}, 1500)
    assert holdings(world, "a") == ({ENERGY: 100}, 500)
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
            replace(world.residents[1], inventory=Inventory(((ENERGY, 29),))),
        ),
    )
    with pytest.raises(ActionRejected) as rejection:
        perform(short, Deliver("b", 1))
    assert rejection.value.code == "not_enough_items"


@pytest.mark.parametrize("action", [Deliver("a", 1), CancelContract("a", 1)])
def test_only_the_supplier_delivers_or_cancels(action: Deliver | CancelContract) -> None:
    with pytest.raises(ActionRejected) as rejection:
        perform(signed(), action)

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


def test_a_delivery_of_the_last_energy_takes_the_supplier_out() -> None:
    world = signed()
    world = replace(
        world,
        residents=(
            world.residents[0],
            replace(world.residents[1], inventory=Inventory(((ENERGY, 30),))),
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

    before = settle_due(replace(world, time=59.9))
    lapsed = settle_due(replace(world, time=60))

    assert next_due(world) == 60
    assert before.contracts.active != ()
    assert lapsed.contracts.active == ()
    assert coin_balance(lapsed, "a") == 1500
    assert next_due(lapsed) is None


def test_a_lapsed_contract_takes_no_more_deliveries_even_before_the_clock_settles() -> None:
    world = replace(signed(), time=60)
    with pytest.raises(ActionRejected) as rejection:
        perform(world, Deliver("b", 1))
    assert rejection.value.code == "unknown_contract"


def test_the_supplier_may_cancel_and_the_buyer_gets_the_rest_back() -> None:
    world = perform(signed(), Deliver("b", 1))
    world = perform(world, CancelContract("b", 1))
    assert world.contracts.active == ()
    assert holdings(world, "a") == ({ENERGY: 40}, 1500)
    assert holdings(world, "b") == ({ENERGY: 70}, 500)


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


# Deposits.

# b stakes 100 coins on each of the 3 deliveries and has just that.
STAKED = replace(BY_SUPPLIER, deposit=100)
DEPOSIT_WORLD = replace(
    WORLD, residents=(WORLD.residents[0], replace(WORLD.residents[1], coins=300))
)


def staked() -> World:
    """b proposes the staked terms and a accepts: contract 1, with deposits, from time 0."""
    world, proposal = send_event(DEPOSIT_WORLD, "b", "a", STAKED)
    return accept_event(world, "a", proposal.id)


def last_end(world: World) -> ContractEnd:
    return world.contracts.ended[-1]


def test_a_supplier_proposing_sets_the_deposit_aside_and_gets_it_back_if_declined() -> None:
    proposed, proposal = send_event(DEPOSIT_WORLD, "b", "a", STAKED)
    declined = reject_event(proposed, "a", proposal.id)

    assert coin_balance(proposed, "b") == 0
    assert contract_coins(proposed, "b") == total_coins(proposed, "b") == 300
    assert coin_balance(declined, "b") == 300


def test_a_buyers_proposal_takes_the_deposit_on_acceptance() -> None:
    world, proposal = send_event(DEPOSIT_WORLD, "a", "b", replace(STAKED, sender_supplies=False))

    signed_world = accept_event(world, "b", proposal.id)

    assert coin_balance(signed_world, "b") == 0
    assert the_contract(signed_world).deposit_held == 300


def test_a_supplier_short_of_the_deposit_cannot_accept_and_the_proposal_stays() -> None:
    world, proposal = send_event(WORLD, "a", "b", replace(STAKED, sender_supplies=False))

    with pytest.raises(InventoryError) as raised:
        accept_event(world, "b", proposal.id)

    assert raised.value.code == "not_enough_coins"
    assert world.events.pending == (proposal,)


def test_each_delivery_hands_back_its_deposit() -> None:
    once = perform(staked(), Deliver("b", 1))
    done = perform(perform(once, Deliver("b", 1)), Deliver("b", 1))

    assert coin_balance(once, "b") == 600
    assert the_contract(once).deposit_held == 200
    assert contract_coins(once, "b") == 200
    assert holdings(done, "b") == ({ENERGY: 10}, 1800)
    end = last_end(done)
    assert (end.reason, end.by, end.forfeit, end.contract.deliveries_left) == (
        "completed",
        "b",
        0,
        0,
    )


@pytest.mark.parametrize(
    ("end_contract", "reason", "by"),
    [
        (lambda world: settle_due(replace(world, time=60)), "lapsed", None),
        (lambda world: perform(world, CancelContract("b", 1)), "cancelled", "b"),
        (lambda world: perform(world, LeaveGame("b")), "party_left", "b"),
    ],
    ids=["lapse", "cancel", "supplier leaves"],
)
def test_deliveries_not_made_cost_the_supplier_their_deposit(
    end_contract: Callable[[World], World], reason: str, by: str | None
) -> None:
    delivered = perform(staked(), Deliver("b", 1))

    world = end_contract(delivered)

    # a gets the 1000 coins left of the payments and b's 200 for the 2 deliveries not made.
    assert coin_balance(world, "a") == 500 + 1000 + 200
    assert coin_balance(world, "b") == 600
    end = last_end(world)
    assert (end.reason, end.by, end.forfeit, end.contract.deliveries_left) == (reason, by, 200, 2)


@pytest.mark.parametrize(
    "end_contract",
    [lambda world: perform(world, LeaveGame("a")), end_game],
    ids=["buyer leaves", "game ends"],
)
def test_the_deposit_goes_back_when_the_supplier_is_not_at_fault(
    end_contract: Callable[[World], World],
) -> None:
    delivered = perform(staked(), Deliver("b", 1))

    world = end_contract(delivered)

    assert coin_balance(world, "a") == 1500
    assert coin_balance(world, "b") == 800
    assert last_end(world).forfeit == 0


def test_the_world_keeps_only_the_latest_endings() -> None:
    world = replace(
        DEPOSIT_WORLD, residents=(replace(WORLD.residents[0], coins=10**6), WORLD.residents[1])
    )
    one_off = replace(BY_BUYER, price=1, deliveries=1)
    for _ in range(CONTRACT_END_LIMIT + 1):
        world, proposal = send_event(world, "a", "b", one_off)
        world = accept_event(world, "b", proposal.id)
        world = perform(world, CancelContract("b", the_contract(world).id))
    ended = world.contracts.ended
    assert len(ended) == CONTRACT_END_LIMIT
    assert ended[0].contract.id == 2


# Through primitives.


def test_propose_contract_sends_a_proposal_either_side_may_make() -> None:
    world = perform(WORLD, ProposeContract("a", "b", False, ENERGY, 30, 500, 3, 60))

    (event,) = world.events.pending
    assert event.payload == BY_BUYER
    assert coin_balance(world, "a") == 500


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (ProposeContract("a", "a", True, ENERGY, 30, 500, 3, 60), "self_addressed"),
        (ProposeContract("a", "b", False, ENERGY, 30, 1000, 3, 60), "not_enough_coins"),
    ],
)
def test_proposals_that_cannot_be_sent_are_rejected(action: ProposeContract, code: str) -> None:
    with pytest.raises(ActionRejected) as rejection:
        perform(WORLD, action)

    assert rejection.value.code == code


SIGNED = signed()
CONTRACT = the_contract(SIGNED)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: replace(SIGNED, ended=True), "outlives its game"),
        (lambda: ContractBook((replace(CONTRACT, deliveries_left=0),), 2), "has 0, must be >= 1"),
        (
            lambda: ContractBook((replace(CONTRACT, expires_at=CONTRACT.signed_at),), 2),
            "lapses before signed",
        ),
        (
            lambda: replace(WORLD, contracts=ContractBook((replace(CONTRACT, buyer_id="b"),), 2)),
            "needs two known residents",
        ),
        (lambda: ContractBook((replace(CONTRACT, deposit=-1),), 2), "has -1, must be >= 0"),
    ],
    ids=["after the end", "nothing left", "expired on signing", "self-dealing", "negative deposit"],
)
def test_the_world_refuses_contracts_that_cannot_be(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


CANCELLED = perform(staked(), CancelContract("b", 1))
END = last_end(CANCELLED)


@pytest.mark.parametrize(
    ("ended", "message"),
    [
        ((replace(END, forfeit=END.contract.deposit_held + 1),), "forfeited more than its deposit"),
        ((replace(END, reason="completed"),), "completed with deliveries left"),
        ((replace(END, time=-1.0),), "ended before signed"),
        ((replace(END, contract=replace(END.contract, id=2)),), "was never given out"),
        ((END, END), "recorded twice"),
    ],
    ids=["forfeit above held", "completed early", "before the start", "unknown id", "twice"],
)
def test_the_contract_book_refuses_endings_that_cannot_be(
    ended: tuple[ContractEnd, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(CANCELLED.contracts, ended=ended)


def test_the_world_refuses_an_ending_by_someone_not_a_party() -> None:
    contracts = replace(CANCELLED.contracts, ended=(replace(END, by="c"),))

    with pytest.raises(ValueError, match="ended contract 1 needs two known residents"):
        replace(CANCELLED, contracts=contracts)


def test_the_world_accepts_an_ending_by_nobody() -> None:
    contracts = replace(CANCELLED.contracts, ended=(replace(END, by=None),))

    assert replace(CANCELLED, contracts=contracts).contracts.ended[0].by is None
