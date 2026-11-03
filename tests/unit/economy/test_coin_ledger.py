"""Every place coins change hands writes the ledger; setting coins aside does not."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.events import pending_for
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.chips import CHIP_MODELS
from aiverse_server.economy.rigs import (
    ENERGY_PER_100,
    RENT_PER_100,
    RIG_PRICE,
    RIG_SECONDS,
    RIG_YIELD,
)
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import (
    Action,
    BuildRig,
    BuyChip,
    CancelContract,
    Deliver,
    FundRig,
    InstallChip,
    LeaveGame,
    OfferInformation,
    OfferToBuy,
    OfferToSell,
    ProposeContract,
    RemoveChip,
    RespondToEvent,
    SellTokens,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game

Row = tuple[str, str, int, str, str | None, int]


def resident(id: str) -> Resident:
    return Resident(
        id, id, "#123456", Position(10, 10), Inventory(((TOKEN, 100), ("MK1", 2))), 10_000
    )


# Everyone stands at the market, which overlaps the pool, so no walking is needed.
WORLD = World(
    WorldMap(100),
    (resident("a"), resident("b")),
    pools=(Pool("p1", Position(10, 10), 4, 1000),),
    market=Market(Position(10, 10), 5),
)

MK1_CYCLE = next(m for m in CHIP_MODELS if m.model == "MK1").tokens_per_job * RIG_YIELD


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


def test_selling_tokens_records_the_market_paying_the_seller() -> None:
    world = perform(WORLD, SellTokens("a", 30))

    coins = next(r.coins for r in world.residents if r.id == "a") - 10_000
    assert rows(world) == [(SYSTEM, "a", coins, "market", TOKEN, 30)]


def test_buying_a_chip_records_the_buyer_paying_the_system() -> None:
    world = perform(WORLD, BuyChip("a", "MK2"))

    assert rows(world) == [("a", SYSTEM, 1_200, "chip_import", "MK2", 1)]


def test_a_rented_rig_cycle_records_rent_to_the_owner_and_electricity_to_the_system() -> None:
    world = run(WORLD, BuildRig("a", "p1"), InstallChip("b", 1, "MK1", 500))
    world = settle_due(replace(world, time=RIG_SECONDS))

    rent = (MK1_CYCLE * RENT_PER_100 + 99) // 100
    energy = (MK1_CYCLE * ENERGY_PER_100 + 99) // 100
    assert rows(world) == [
        ("a", SYSTEM, RIG_PRICE, "rig_build", None, 0),
        ("b", "a", rent, "rent", None, 0),
        ("b", SYSTEM, energy, "electricity", None, 0),
    ]
    assert (rent, energy) == (30, 12)


def test_the_owner_using_their_own_rig_pays_only_electricity() -> None:
    world = run(WORLD, BuildRig("a", "p1"), InstallChip("a", 1, "MK1", 500))
    world = settle_due(replace(world, time=RIG_SECONDS))

    assert [row[3] for row in rows(world)] == ["rig_build", "electricity"]


@pytest.mark.parametrize(
    ("offer", "recipient"),
    [(OfferToBuy("a", "b", TOKEN, 20, 300), "b"), (OfferToSell("b", "a", TOKEN, 20, 300), "a")],
)
def test_an_accepted_offer_records_the_buyer_paying_the_seller(
    offer: Action, recipient: str
) -> None:
    world = accept(perform(WORLD, offer), recipient)

    assert rows(world) == [("a", "b", 300, "offer", TOKEN, 20)]


def signed() -> World:
    """b is to supply a twice with 10 tokens for 200 coins each, staking 50 per delivery."""
    return accept(perform(WORLD, ProposeContract("b", "a", True, TOKEN, 10, 200, 2, 60, 50)), "a")


def test_a_contract_delivery_records_the_buyer_paying_the_supplier() -> None:
    world = perform(signed(), Deliver("b", 1))

    assert rows(world) == [("a", "b", 200, "contract_payment", TOKEN, 10)]


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
    world = accept(perform(WORLD, OfferInformation("a", "b", "tip", "pool p1 is rich", 40)), "b")

    assert rows(world) == [("b", "a", 40, "information", None, 0)]


def test_coins_set_aside_and_handed_back_record_nothing() -> None:
    world = run(
        WORLD,
        OfferToBuy("a", "b", TOKEN, 20, 300),
        WithdrawEvent("a", 1),
        ProposeContract("b", "a", False, TOKEN, 10, 200, 2, 60),
        WithdrawEvent("b", 2),
        BuildRig("a", "p1"),
        InstallChip("a", 1, "MK1", 500),
        FundRig("a", 1, 1, 100),
        RemoveChip("a", 1, 1),
    )

    assert [row[3] for row in rows(world)] == ["rig_build"]


def rented() -> World:
    return run(WORLD, BuildRig("a", "p1"), InstallChip("b", 1, "MK1", 500))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (
            lambda: perform(WORLD, OfferToBuy("a", "b", TOKEN, 20, 300)),
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
        "rig_tenant_left",
        "rig_owner_left",
        "rig_game_ended",
    ],
)
def test_coins_handed_back_when_a_deal_ends_record_nothing(
    start: Callable[[], World], end: Callable[[World], World]
) -> None:
    before = start()

    after = end(before)

    assert after.ledger == before.ledger
