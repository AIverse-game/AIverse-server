"""Every place coins change hands writes the ledger; setting coins aside does not."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.events import pending_for
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Inventory, Market, Position, Resident, Spring, World, WorldMap
from aiverse_server.economy.crystals import CRYSTAL_TIERS
from aiverse_server.economy.energy import ENERGY
from aiverse_server.economy.towers import (
    RENT_PER_100,
    TOWER_PRICE,
    TOWER_SECONDS,
    TOWER_YIELD,
    UPKEEP_PER_100,
)
from aiverse_server.game.actions import (
    Action,
    BuildTower,
    BuyCrystal,
    CancelContract,
    Deliver,
    FundTower,
    InstallCrystal,
    LeaveGame,
    OfferInformation,
    OfferToBuy,
    OfferToSell,
    ProposeContract,
    RemoveCrystal,
    RespondToEvent,
    SellEnergy,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game

Row = tuple[str, str, int, str, str | None, int]


def resident(id: str) -> Resident:
    return Resident(
        id, id, "#123456", Position(10, 10), Inventory(((ENERGY, 100), ("C1", 2))), 10_000
    )


# Everyone stands at the market, which overlaps the spring, so no walking is needed.
WORLD = World(
    WorldMap(100),
    (resident("a"), resident("b")),
    springs=(Spring("p1", Position(10, 10), 4, 1000),),
    market=Market(Position(10, 10), 5),
)

C1_CYCLE = next(m for m in CRYSTAL_TIERS if m.tier == "C1").energy_per_job * TOWER_YIELD


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


def test_selling_energy_records_the_market_paying_the_seller() -> None:
    world = perform(WORLD, SellEnergy("a", 30))

    coins = next(r.coins for r in world.residents if r.id == "a") - 10_000
    assert rows(world) == [(SYSTEM, "a", coins, "market", ENERGY, 30)]


def test_buying_a_crystal_records_the_buyer_paying_the_system() -> None:
    world = perform(WORLD, BuyCrystal("a", "C2"))

    assert rows(world) == [("a", SYSTEM, 2_400, "crystal_import", "C2", 1)]


def test_a_rented_tower_cycle_records_rent_to_the_owner_and_upkeep_to_the_system() -> None:
    world = run(WORLD, BuildTower("a", "p1"), InstallCrystal("b", 1, "C1", 500))
    world = settle_due(replace(world, time=TOWER_SECONDS))

    rent = (C1_CYCLE * RENT_PER_100 + 99) // 100
    upkeep = (C1_CYCLE * UPKEEP_PER_100 + 99) // 100
    assert rows(world) == [
        ("a", SYSTEM, TOWER_PRICE, "tower_build", None, 0),
        ("b", "a", rent, "rent", None, 0),
        ("b", SYSTEM, upkeep, "upkeep", None, 0),
    ]
    assert (rent, upkeep) == (30, 12)


def test_the_owner_using_their_own_tower_pays_only_upkeep() -> None:
    world = run(WORLD, BuildTower("a", "p1"), InstallCrystal("a", 1, "C1", 500))
    world = settle_due(replace(world, time=TOWER_SECONDS))

    assert [row[3] for row in rows(world)] == ["tower_build", "upkeep"]


@pytest.mark.parametrize(
    ("offer", "recipient"),
    [(OfferToBuy("a", "b", ENERGY, 20, 300), "b"), (OfferToSell("b", "a", ENERGY, 20, 300), "a")],
)
def test_an_accepted_offer_records_the_buyer_paying_the_seller(
    offer: Action, recipient: str
) -> None:
    world = accept(perform(WORLD, offer), recipient)

    assert rows(world) == [("a", "b", 300, "offer", ENERGY, 20)]


def signed() -> World:
    """b is to supply a twice with 10 energy for 200 coins each, staking 50 per delivery."""
    return accept(perform(WORLD, ProposeContract("b", "a", True, ENERGY, 10, 200, 2, 60, 50)), "a")


def test_a_contract_delivery_records_the_buyer_paying_the_supplier() -> None:
    world = perform(signed(), Deliver("b", 1))

    assert rows(world) == [("a", "b", 200, "contract_payment", ENERGY, 10)]


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
        OfferToBuy("a", "b", ENERGY, 20, 300),
        WithdrawEvent("a", 1),
        ProposeContract("b", "a", False, ENERGY, 10, 200, 2, 60),
        WithdrawEvent("b", 2),
        BuildTower("a", "p1"),
        InstallCrystal("a", 1, "C1", 500),
        FundTower("a", 1, 1, 100),
        RemoveCrystal("a", 1, 1),
    )

    assert [row[3] for row in rows(world)] == ["tower_build"]


def rented() -> World:
    return run(WORLD, BuildTower("a", "p1"), InstallCrystal("b", 1, "C1", 500))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (
            lambda: perform(WORLD, OfferToBuy("a", "b", ENERGY, 20, 300)),
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
        "tower_tenant_left",
        "tower_owner_left",
        "tower_game_ended",
    ],
)
def test_coins_handed_back_when_a_deal_ends_record_nothing(
    start: Callable[[], World], end: Callable[[World], World]
) -> None:
    before = start()

    after = end(before)

    assert after.ledger == before.ledger
