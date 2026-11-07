import json
from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy import exchange as exchange_rules
from aiverse_server.game.actions import (
    OfferToSell,
    PlaceOrder,
    ProposeContract,
    RespondToEvent,
    Say,
    perform,
)
from aiverse_server.server import protocol
from aiverse_server.server.protocol import (
    ERROR_MAX_LENGTH,
    SPEECH_VIEW_LIMIT,
    FillView,
    LevelView,
    OrderView,
    bad_message,
    parse_client_message,
    world_view,
)

POSTER = "poster"
SIGN = "sign"
STOCK = Inventory(((POSTER, 100), (SIGN, 20)))


@pytest.fixture(autouse=True)
def books(monkeypatch: pytest.MonkeyPatch) -> None:
    """No item has a book in the game yet; give these two one."""
    monkeypatch.setattr(exchange_rules, "EXCHANGE_ITEMS", (POSTER, SIGN))
    monkeypatch.setattr(protocol, "EXCHANGE_ITEMS", (POSTER, SIGN))


# c joins a and b, so a whisper between a and b has someone to be kept from.
WORLD = World(
    map=WorldMap(size=50),
    residents=(
        Resident(id="a", name="甲", color="#112233", position=Position(0, 50), inventory=STOCK),
        Resident(id="b", name="乙", color="#AABBCC", position=Position(12.5, 7), inventory=STOCK),
        Resident(id="c", name="丙", color="#445566", position=Position(25, 25), inventory=STOCK),
    ),
)


def resident_view(world: dict[str, Any], resident_id: str) -> dict[str, Any]:
    return next(r for r in world["residents"] if r["id"] == resident_id)


def test_error_details_are_truncated() -> None:
    message = bad_message("x" * 500).message
    assert len(message) == ERROR_MAX_LENGTH
    assert message.endswith("…")


def test_a_parsed_request_keeps_the_key_out_of_its_repr() -> None:
    llm = {"baseUrl": "https://page.example/v1", "apiKey": "page-key", "model": "page-model"}
    message = parse_client_message(
        json.dumps({"type": "start_agent", "residentId": "a", "llm": llm})
    )
    assert "page-model" in repr(message)
    assert "page-key" not in repr(message)


def test_states_show_only_the_viewers_offers_and_contracts() -> None:
    rich = tuple(replace(resident, coins=1000) for resident in WORLD.residents)
    world = replace(WORLD, residents=rich)
    # b offers some posters, a proposes b a contract and b signs it, then a proposes another.
    world = perform(world, OfferToSell("b", "a", POSTER, 5, 80))
    world = perform(world, ProposeContract("a", "b", False, POSTER, 10, 100, 3, 60, deposit=20))
    world = perform(world, RespondToEvent("b", 2, True))
    world = perform(
        replace(world, time=15), ProposeContract("b", "a", True, SIGN, 1, 200, 1, 30, deposit=50)
    )
    shown = world_view(world, 100, viewer="a").model_dump(by_alias=True, exclude_none=True)
    assert shown["offers"] == [
        {
            "eventId": 1,
            "side": "sell",
            "fromId": "b",
            "toId": "a",
            "item": POSTER,
            "quantity": 5,
            "price": 80,
        }
    ]
    assert shown["contractProposals"] == [
        {
            "eventId": 3,
            "fromId": "b",
            "toId": "a",
            "supplierId": "b",
            "buyerId": "a",
            "item": SIGN,
            "quantity": 1,
            "price": 200,
            "deliveries": 1,
            "durationSeconds": 30,
            "deposit": 50,
        }
    ]
    assert shown["contracts"] == [
        {
            "id": 1,
            "supplierId": "b",
            "buyerId": "a",
            "item": POSTER,
            "quantity": 10,
            "price": 100,
            "deliveriesLeft": 3,
            "escrow": 300,
            "deposit": 20,
            "depositHeld": 60,
            "remainingSeconds": 45.0,
        }
    ]
    # The escrow still counts in a's public total, b's deposits in b's.
    assert resident_view(shown, "a")["coins"] == 1000
    assert resident_view(shown, "b")["coins"] == 1000
    for outsider in ("c", None):
        hidden = world_view(world, 100, viewer=outsider)
        assert (hidden.offers, hidden.contract_proposals, hidden.contracts) == ([], [], [])


def test_the_latest_lines_are_counted_among_those_the_viewer_may_see() -> None:
    world = WORLD
    world = perform(world, Say("a", "aloud"))
    for i in range(SPEECH_VIEW_LIMIT):
        world = perform(world, Say("b", str(i), to="c"))
    # a sees no whisper here, so the line aloud is still among a's latest.
    seen = world_view(world, 1, "a").speech
    assert [line.text for line in seen] == ["aloud"]
    assert len(world_view(world, 1, "c").speech) == SPEECH_VIEW_LIMIT


TRADE_WORLD = replace(
    WORLD, residents=tuple(replace(resident, coins=1000) for resident in WORLD.residents)
)


def test_residents_show_their_public_state_and_no_inventory() -> None:
    world = perform(TRADE_WORLD, OfferToSell("a", "b", SIGN, 1, 50))

    shown = world_view(world, 1).model_dump(by_alias=True, exclude_none=True)

    assert resident_view(shown, "a") == {
        "id": "a",
        "name": "甲",
        "color": "#112233",
        "position": {"x": 0, "y": 50},
        "coins": 1000,
        "spendableCoins": 1000,
        "left": False,
        "rank": 1,
    }


def test_the_exchange_has_no_books_while_no_item_is_traded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "EXCHANGE_ITEMS", ())

    exchange = world_view(TRADE_WORLD, 1).exchange

    assert exchange.books == []


def test_the_exchange_shows_books_and_fills_without_names() -> None:
    world = perform(TRADE_WORLD, PlaceOrder("a", "sell", SIGN, 8, 30))
    world = perform(world, PlaceOrder("b", "buy", SIGN, 3, 32))
    world = perform(world, PlaceOrder("b", "buy", SIGN, 2, 25))

    exchange = world_view(world, 1, viewer="c").exchange

    data = next(book for book in exchange.books if book.item == SIGN)
    assert (data.bids, data.asks) == (
        [LevelView(price=25, quantity=2)],
        [LevelView(price=30, quantity=5)],
    )
    assert exchange.fills == [FillView(seq=1, time=0, item=SIGN, quantity=3, price=30)]


def test_the_exchange_marks_the_viewers_own_fills() -> None:
    world = perform(TRADE_WORLD, PlaceOrder("a", "sell", SIGN, 8, 30))
    world = perform(world, PlaceOrder("b", "buy", SIGN, 3, 32))

    seen = {viewer: world_view(world, 1, viewer=viewer).exchange for viewer in ("a", "b", "c")}

    assert [seen[viewer].fills[0].own for viewer in ("a", "b", "c")] == ["sold", "bought", None]


def test_the_exchange_lists_the_viewers_orders_and_names_the_viewer() -> None:
    world = perform(TRADE_WORLD, PlaceOrder("a", "sell", SIGN, 8, 30))

    exchange = world_view(replace(world, time=100), 1, viewer="a").exchange

    assert exchange.viewer_id == "a"
    assert exchange.orders == [
        OrderView(id=1, side="sell", item=SIGN, quantity=8, price=30, remaining_seconds=200)
    ]


@pytest.mark.parametrize("viewer", ["c", None], ids=["someone_else", "nobody"])
def test_the_exchange_lists_no_orders_for_another_viewer(viewer: str | None) -> None:
    world = perform(TRADE_WORLD, PlaceOrder("a", "sell", SIGN, 8, 30))

    exchange = world_view(world, 1, viewer=viewer).exchange

    assert (exchange.orders, exchange.viewer_id) == ([], viewer)


def test_fill_times_are_sent_to_a_tenth_of_a_second() -> None:
    world = perform(
        replace(TRADE_WORLD, time=74.07406799999997), PlaceOrder("a", "sell", SIGN, 8, 30)
    )
    world = perform(world, PlaceOrder("b", "buy", SIGN, 3, 32))

    exchange = world_view(world, 1).exchange

    assert exchange.fills[0].time == 74.1
