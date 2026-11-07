"""Every place coins change hands writes the ledger; setting coins aside does not."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.events import pending_for
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.game.actions import (
    Action,
    BuyParcel,
    CancelContract,
    Deliver,
    LeaveGame,
    OfferInformation,
    OfferToBuy,
    OfferToSell,
    ProposeContract,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game

POSTER = "poster"

Row = tuple[str, str, int, str, str | None, int]


def resident(id: str) -> Resident:
    return Resident(id, id, "#123456", Position(10, 10), Inventory(((POSTER, 100),)), 10_000)


WORLD = World(WorldMap(100), (resident("a"), resident("b")))


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


def test_buying_a_parcel_records_the_buyer_paying_the_system() -> None:
    world = perform(WORLD, BuyParcel("a"))

    assert rows(world) == [("a", SYSTEM, 1_550, "land", None, 0)]


@pytest.mark.parametrize(
    ("offer", "recipient"),
    [(OfferToBuy("a", "b", POSTER, 20, 300), "b"), (OfferToSell("b", "a", POSTER, 20, 300), "a")],
)
def test_an_accepted_offer_records_the_buyer_paying_the_seller(
    offer: Action, recipient: str
) -> None:
    world = accept(perform(WORLD, offer), recipient)

    assert rows(world) == [("a", "b", 300, "offer", POSTER, 20)]


def signed() -> World:
    """b is to supply a twice with 10 posters for 200 coins each, staking 50 per delivery."""
    return accept(perform(WORLD, ProposeContract("b", "a", True, POSTER, 10, 200, 2, 60, 50)), "a")


def test_a_contract_delivery_records_the_buyer_paying_the_supplier() -> None:
    world = perform(signed(), Deliver("b", 1))

    assert rows(world) == [("a", "b", 200, "contract_payment", POSTER, 10)]


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
    world = accept(perform(WORLD, OfferInformation("a", "b", "tip", "the plaza is busy", 40)), "b")

    assert rows(world) == [("b", "a", 40, "information", None, 0)]


def test_coins_set_aside_and_handed_back_record_nothing() -> None:
    world = run(
        WORLD,
        OfferToBuy("a", "b", POSTER, 20, 300),
        WithdrawEvent("a", 1),
        ProposeContract("b", "a", False, POSTER, 10, 200, 2, 60),
        WithdrawEvent("b", 2),
    )

    assert rows(world) == []


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (
            lambda: perform(WORLD, OfferToBuy("a", "b", POSTER, 20, 300)),
            lambda world: perform(world, RespondToEvent("b", 1, False)),
        ),
        (signed, lambda world: perform(world, LeaveGame("a"))),
        (signed, end_game),
    ],
    ids=["offer_declined", "contract_buyer_left", "contract_game_ended"],
)
def test_coins_handed_back_when_a_deal_ends_record_nothing(
    start: Callable[[], World], end: Callable[[World], World]
) -> None:
    before = start()

    after = end(before)

    assert after.ledger == before.ledger
