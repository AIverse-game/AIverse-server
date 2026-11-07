"""Every place coins change hands writes the ledger; setting coins aside does not."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.events import pending_for
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Inventory, Market, Position, Resident, Spring, World, WorldMap
from aiverse_server.economy.collectors import (
    COLLECTOR_PRICE,
    COLLECTOR_SECONDS,
    COLLECTOR_YIELD,
    RENT_PER_100,
    UPKEEP_PER_100,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.programs import PROGRAM_TIERS
from aiverse_server.game.actions import (
    Action,
    BuildCollector,
    BuyProgram,
    CancelContract,
    Deliver,
    FundCollector,
    InstallProgram,
    LeaveGame,
    OfferInformation,
    OfferToBuy,
    OfferToSell,
    ProposeContract,
    RemoveProgram,
    RespondToEvent,
    SellCompute,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game

Row = tuple[str, str, int, str, str | None, int]


def resident(id: str) -> Resident:
    return Resident(
        id, id, "#123456", Position(10, 10), Inventory(((COMPUTE, 100), ("v1", 2))), 10_000
    )


# Everyone stands at the market, which overlaps the spring, so no walking is needed.
WORLD = World(
    WorldMap(100),
    (resident("a"), resident("b")),
    springs=(Spring("p1", Position(10, 10), 4, 1000),),
    market=Market(Position(10, 10), 5),
)

V1_CYCLE = next(m for m in PROGRAM_TIERS if m.tier == "v1").compute_per_job * COLLECTOR_YIELD


def rows(world: World) -> list[Row]:
    return [
        (t.payer, t.payee, t.amount, t.channel, t.item, t.quantity) for t in world.ledger.transfers
    ]


def run(world: World, *actions: Action) -> World:
    for action in actions:
        world = perform(world, action)
    return world


def accept(world: World, resident_id: str) -> World:
    return perform(world, RespondToEvent(resident_id, pending_for(world, resident_id)[0].id, True))


def test_selling_compute_records_the_market_paying_the_seller() -> None:
    world = perform(WORLD, SellCompute("a", 30))

    coins = next(r.coins for r in world.residents if r.id == "a") - 10_000
    assert rows(world) == [(SYSTEM, "a", coins, "market", COMPUTE, 30)]


def test_buying_a_program_records_the_buyer_paying_the_system() -> None:
    world = perform(WORLD, BuyProgram("a", "v2"))

    assert rows(world) == [("a", SYSTEM, 2_400, "program_import", "v2", 1)]


def test_a_rented_collector_cycle_records_rent_to_the_owner_and_upkeep_to_the_system() -> None:
    world = run(WORLD, BuildCollector("a", "p1"), InstallProgram("b", 1, "v1", 500))
    world = settle_due(replace(world, time=COLLECTOR_SECONDS))

    rent = (V1_CYCLE * RENT_PER_100 + 99) // 100
    upkeep = (V1_CYCLE * UPKEEP_PER_100 + 99) // 100
    assert rows(world) == [
        ("a", SYSTEM, COLLECTOR_PRICE, "collector_build", None, 0),
        ("b", "a", rent, "rent", None, 0),
        ("b", SYSTEM, upkeep, "upkeep", None, 0),
    ]
    assert (rent, upkeep) == (30, 12)


def test_the_owner_using_their_own_collector_pays_only_upkeep() -> None:
    world = run(WORLD, BuildCollector("a", "p1"), InstallProgram("a", 1, "v1", 500))
    world = settle_due(replace(world, time=COLLECTOR_SECONDS))

    assert [row[3] for row in rows(world)] == ["collector_build", "upkeep"]


@pytest.mark.parametrize(
    ("offer", "recipient"),
    [(OfferToBuy("a", "b", COMPUTE, 20, 300), "b"), (OfferToSell("b", "a", COMPUTE, 20, 300), "a")],
)
def test_an_accepted_offer_records_the_buyer_paying_the_seller(
    offer: Action, recipient: str
) -> None:
    world = accept(perform(WORLD, offer), recipient)

    assert rows(world) == [("a", "b", 300, "offer", COMPUTE, 20)]


def signed() -> World:
    """b is to supply a twice with 10 compute for 200 coins each, staking 50 per delivery."""
    return accept(perform(WORLD, ProposeContract("b", "a", True, COMPUTE, 10, 200, 2, 60, 50)), "a")


def test_a_contract_delivery_records_the_buyer_paying_the_supplier() -> None:
    world = perform(signed(), Deliver("b", 1))

    assert rows(world) == [("a", "b", 200, "contract_payment", COMPUTE, 10)]


@pytest.mark.parametrize(
    ("breach", "forfeit"),
    [
        (lambda world: settle_due(replace(perform(world, Deliver("b", 1)), time=60)), 50),
        (lambda world: perform(world, CancelContract("b", 1)), 100),
        (lambda world: perform(world, LeaveGame("b")), 100),
    ],
    ids=["lapsed", "cancelled", "supplier_left"],
)
def test_a_breached_contract_records_the_supplier_forfeiting_the_undelivered_deposit(
    breach: Callable[[World], World], forfeit: int
) -> None:
    world = breach(signed())

    assert rows(world)[-1] == ("b", "a", forfeit, "contract_forfeit", None, 0)


def test_selling_information_records_the_buyer_paying_the_seller() -> None:
    world = accept(perform(WORLD, OfferInformation("a", "b", "tip", "spring p1 is rich", 40)), "b")

    assert rows(world) == [("b", "a", 40, "information", None, 0)]


def test_coins_set_aside_and_handed_back_record_nothing() -> None:
    world = run(
        WORLD,
        OfferToBuy("a", "b", COMPUTE, 20, 300),
        WithdrawEvent("a", 1),
        ProposeContract("b", "a", False, COMPUTE, 10, 200, 2, 60),
        WithdrawEvent("b", 2),
        BuildCollector("a", "p1"),
        InstallProgram("a", 1, "v1", 500),
        FundCollector("a", 1, 1, 100),
        RemoveProgram("a", 1, 1),
    )

    assert [row[3] for row in rows(world)] == ["collector_build"]


def rented() -> World:
    return run(WORLD, BuildCollector("a", "p1"), InstallProgram("b", 1, "v1", 500))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (
            lambda: perform(WORLD, OfferToBuy("a", "b", COMPUTE, 20, 300)),
            lambda world: perform(world, RespondToEvent("b", 1, False)),
        ),
        (signed, lambda world: perform(world, LeaveGame("a"))),
        (signed, end_game),
        (rented, lambda world: perform(world, LeaveGame("b"))),
        (rented, lambda world: perform(world, LeaveGame("a"))),
        (rented, end_game),
    ],
    ids=[
        "offer_declined",
        "contract_buyer_left",
        "contract_game_ended",
        "collector_tenant_left",
        "collector_owner_left",
        "collector_game_ended",
    ],
)
def test_coins_handed_back_when_a_deal_ends_record_nothing(
    start: Callable[[], World], end: Callable[[World], World]
) -> None:
    before = start()

    after = end(before)

    assert after.ledger == before.ledger
