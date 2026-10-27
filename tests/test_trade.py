import pytest

from aiverse_server.actions import ActionRejected, OfferToBuy, OfferToSell, RespondToEvent, perform
from aiverse_server.events import (
    EventError,
    accept_event,
    pending_for,
    reject_event,
    send_event,
    withdraw_event,
)
from aiverse_server.inventory import InventoryError, list_items, store_item
from aiverse_server.trade import COIN, MAX_AMOUNT, PurchaseOffer, SaleOffer, TradeError
from aiverse_server.world import Inventory, Position, Resident, World, WorldMap

# a has coins and wants bread; b bakes bread and has no coins.
WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(
            id="a",
            name="a",
            color="#ffffff",
            position=Position(1, 1),
            inventory=Inventory(((COIN, 20),)),
        ),
        Resident(
            id="b",
            name="b",
            color="#000000",
            position=Position(9, 9),
            inventory=Inventory((("bread", 5),)),
        ),
    ),
)


def test_a_purchase_offer_sets_the_coins_aside_and_completes_on_accept() -> None:
    world, offer = send_event(WORLD, "a", "b", PurchaseOffer("bread", 3, 10))
    assert list_items(world, "a") == {COIN: 10}
    assert list_items(world, "b") == {"bread": 5}
    world = accept_event(world, "b", offer.id)
    assert list_items(world, "a") == {"bread": 3, COIN: 10}
    assert list_items(world, "b") == {"bread": 2, COIN: 10}
    assert world.events.pending == ()


def test_a_sale_offer_sets_the_items_aside_and_completes_on_accept() -> None:
    world, offer = send_event(WORLD, "b", "a", SaleOffer("bread", 3, 10))
    assert list_items(world, "a") == {COIN: 20}
    assert list_items(world, "b") == {"bread": 2}
    world = accept_event(world, "a", offer.id)
    assert list_items(world, "a") == {"bread": 3, COIN: 10}
    assert list_items(world, "b") == {"bread": 2, COIN: 10}
    assert world.events.pending == ()


@pytest.mark.parametrize("decline", ["reject", "withdraw"])
@pytest.mark.parametrize(
    ("sender", "recipient", "offer"),
    [("a", "b", PurchaseOffer("bread", 3, 10)), ("b", "a", SaleOffer("bread", 3, 10))],
)
def test_declining_gives_the_sender_back_what_was_set_aside(
    decline: str, sender: str, recipient: str, offer: PurchaseOffer | SaleOffer
) -> None:
    world, event = send_event(WORLD, sender, recipient, offer)
    if decline == "reject":
        world = reject_event(world, recipient, event.id)
    else:
        world = withdraw_event(world, sender, event.id)
    assert world.residents == WORLD.residents
    assert world.events.pending == ()


@pytest.mark.parametrize(
    ("sender", "recipient", "offer"),
    [("a", "b", PurchaseOffer("bread", 1, 21)), ("b", "a", SaleOffer("bread", 6, 1))],
)
def test_a_sender_cannot_offer_more_than_they_hold(
    sender: str, recipient: str, offer: PurchaseOffer | SaleOffer
) -> None:
    with pytest.raises(InventoryError) as raised:
        send_event(WORLD, sender, recipient, offer)
    assert raised.value.code == "not_enough_items"


@pytest.mark.parametrize(
    ("sender", "recipient", "offer"),
    [("a", "b", PurchaseOffer("bread", 6, 10)), ("b", "a", SaleOffer("bread", 1, 21))],
)
def test_a_recipient_short_of_their_side_leaves_the_offer_pending(
    sender: str, recipient: str, offer: PurchaseOffer | SaleOffer
) -> None:
    world, event = send_event(WORLD, sender, recipient, offer)
    with pytest.raises(InventoryError) as raised:
        accept_event(world, recipient, event.id)
    assert raised.value.code == "not_enough_items"
    assert pending_for(world, recipient) == (event,)
    # Once they have enough, the same offer goes through.
    topped_up = store_item(store_item(world, "a", COIN, 1), "b", "bread")
    assert accept_event(topped_up, recipient, event.id).events.pending == ()


def test_offers_are_sent_before_the_recipient_can_pay() -> None:
    # b holds no coins yet; the offer still goes out, since b may earn them before answering.
    world, event = send_event(WORLD, "b", "a", SaleOffer("bread", 1, 1))
    assert pending_for(world, "a") == (event,)
    world, event = send_event(WORLD, "a", "b", PurchaseOffer("wood", 1, 1))
    assert pending_for(world, "b") == (event,)


@pytest.mark.parametrize("payload", [PurchaseOffer, SaleOffer])
@pytest.mark.parametrize(
    ("kind", "quantity", "price"),
    [
        ("", 1, 1),
        (5, 1, 1),
        (COIN, 1, 1),
        ("bread", 0, 1),
        ("bread", True, 1),
        ("bread", 1.0, 1),
        pytest.param("bread", 10**5000, 1, id="huge-quantity"),
        ("bread", 1, 0),
        ("bread", 1, True),
        ("bread", 1, 2.5),
        pytest.param("bread", 1, -(10**5000), id="huge-negative-price"),
        ("bread", 1, MAX_AMOUNT + 1),
    ],
)
def test_terms_that_cannot_make_a_trade_are_refused(
    payload: type[PurchaseOffer | SaleOffer], kind: object, quantity: object, price: object
) -> None:
    with pytest.raises(TradeError) as raised:
        send_event(WORLD, "a", "b", payload(kind, quantity, price))  # type: ignore[arg-type]
    assert raised.value.code == "invalid_trade"


@pytest.mark.parametrize(
    ("sender", "recipient", "offer"),
    [("a", "b", PurchaseOffer("bread", 1, 15)), ("b", "a", SaleOffer("bread", 4, 1))],
)
def test_what_is_set_aside_cannot_be_offered_twice(
    sender: str, recipient: str, offer: PurchaseOffer | SaleOffer
) -> None:
    world, _ = send_event(WORLD, sender, recipient, offer)
    with pytest.raises(InventoryError) as raised:
        send_event(world, sender, recipient, offer)
    assert raised.value.code == "not_enough_items"


def test_residents_cannot_trade_with_themselves() -> None:
    with pytest.raises(EventError) as raised:
        send_event(WORLD, "a", "a", PurchaseOffer("bread", 1, 1))
    assert raised.value.code == "self_addressed"


def test_offer_primitives_complete_a_trade_from_either_side() -> None:
    buy = OfferToBuy(resident_id="a", seller_id="b", kind="bread", quantity=2, price=6)
    sell = OfferToSell(resident_id="b", buyer_id="a", kind="bread", quantity=2, price=6)
    results = []
    offers: list[tuple[OfferToBuy | OfferToSell, str]] = [(buy, "b"), (sell, "a")]
    for offer, recipient in offers:
        world = perform(WORLD, offer)
        (event,) = pending_for(world, recipient)
        results.append(perform(world, RespondToEvent(recipient, event.id, accept=True)))
    assert results[0] == results[1]
    assert list_items(results[0], "a") == {"bread": 2, COIN: 14}
    assert list_items(results[0], "b") == {"bread": 3, COIN: 6}


@pytest.mark.parametrize(
    ("offer", "code"),
    [
        (OfferToBuy("a", "b", "bread", 1, 21), "not_enough_items"),
        (OfferToSell("b", "a", "bread", 1, 0), "invalid_trade"),
        (OfferToBuy("a", "a", "bread", 1, 1), "self_addressed"),
        (OfferToSell("b", "nobody", "bread", 1, 1), "unknown_resident"),
    ],
)
def test_refused_offers_become_rejections(offer: OfferToBuy | OfferToSell, code: str) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, offer)
    assert raised.value.code == code


def test_accepting_without_the_goods_becomes_a_rejection() -> None:
    world = perform(WORLD, OfferToBuy("a", "b", "bread", 6, 10))
    (event,) = pending_for(world, "b")
    with pytest.raises(ActionRejected) as raised:
        perform(world, RespondToEvent("b", event.id, accept=True))
    assert raised.value.code == "not_enough_items"
