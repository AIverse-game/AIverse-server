import json
from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.core.world import (
    Inventory,
    Market,
    Mine,
    Position,
    Resident,
    Studio,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.gathering import DATA
from aiverse_server.game.actions import (
    GatherData,
    OfferToSell,
    PlaceOrder,
    ProposeContract,
    RespondToEvent,
    Say,
    WriteProgram,
    perform,
)
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

STOCK = Inventory(((COMPUTE, 100), ("v1", 1)))

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


def test_a_program_set_aside_in_an_offer_still_shows_as_owned() -> None:
    world = perform(WORLD, OfferToSell("a", "b", "v1", 1, 1))
    assert resident_view(world_view(world, 1).model_dump(by_alias=True), "a")["programs"] == {
        "v1": 1
    }


def test_states_show_only_the_viewers_offers_and_contracts() -> None:
    rich = tuple(replace(resident, coins=1000) for resident in WORLD.residents)
    world = replace(WORLD, residents=rich)
    # b offers some compute, a proposes b a contract and b signs it, then a proposes another.
    world = perform(world, OfferToSell("b", "a", COMPUTE, 5, 80))
    world = perform(world, ProposeContract("a", "b", False, COMPUTE, 10, 100, 3, 60, deposit=20))
    world = perform(world, RespondToEvent("b", 2, True))
    world = perform(
        replace(world, time=15), ProposeContract("b", "a", True, "v1", 1, 200, 1, 30, deposit=50)
    )
    shown = world_view(world, 100, viewer="a").model_dump(by_alias=True, exclude_none=True)
    assert shown["offers"] == [
        {
            "eventId": 1,
            "side": "sell",
            "fromId": "b",
            "toId": "a",
            "item": COMPUTE,
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
            "item": "v1",
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
            "item": COMPUTE,
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


# Everyone stands where they can gather, write and trade, with the data for a v1.
SITE = Position(25, 25)
CRAFT_STOCK = Inventory(((COMPUTE, 100), (DATA, 20)))
CRAFT_WORLD = replace(
    WORLD,
    residents=tuple(
        replace(resident, position=SITE, inventory=CRAFT_STOCK, coins=1000)
        for resident in WORLD.residents
    ),
    mines=(Mine("m1", SITE, 4, 300),),
    studio=Studio(SITE, 5),
    market=Market(SITE, 5),
)


def test_a_gatherer_and_a_writer_show_their_jobs() -> None:
    world = perform(CRAFT_WORLD, GatherData("a", "m1"))
    world = perform(world, WriteProgram("b", "v1"))

    shown = world_view(replace(world, time=4), 1).model_dump(by_alias=True, exclude_none=True)

    gatherer, writer = resident_view(shown, "a"), resident_view(shown, "b")
    assert gatherer["gathering"] == {"mineId": "m1", "amount": 6, "remainingSeconds": 6.0}
    assert writer["writing"] == {"tier": "v1", "remainingSeconds": 11.0}
    assert shown["mines"] == [
        {"id": "m1", "position": {"x": 25, "y": 25}, "radius": 4, "reserve": 294}
    ]


def test_the_studio_shows_its_recipes_and_speedup() -> None:
    shown = world_view(CRAFT_WORLD, 1).model_dump(by_alias=True, exclude_none=True)

    studio = shown["studio"]
    assert studio["recipes"][:2] == [
        {"tier": "v1", "data": 5, "compute": 15, "seconds": 15.0},
        {"tier": "v2", "data": 15, "compute": 40, "consumes": "v1", "seconds": 30.0},
    ]
    assert (studio["speedupPerProgram"], studio["fastestShare"]) == (0.1, 0.5)


def test_data_set_aside_in_a_sell_order_still_shows_as_owned() -> None:
    world = perform(CRAFT_WORLD, PlaceOrder("a", "sell", DATA, 8, 30))

    shown = world_view(world, 1).model_dump(by_alias=True)

    assert resident_view(shown, "a")["data"] == 20


def test_the_exchange_shows_books_and_fills_without_names() -> None:
    world = perform(CRAFT_WORLD, PlaceOrder("a", "sell", DATA, 8, 30))
    world = perform(world, PlaceOrder("b", "buy", DATA, 3, 32))
    world = perform(world, PlaceOrder("b", "buy", DATA, 2, 25))

    exchange = world_view(world, 1, viewer="c").exchange

    data = next(book for book in exchange.books if book.item == DATA)
    assert (data.bids, data.asks) == (
        [LevelView(price=25, quantity=2)],
        [LevelView(price=30, quantity=5)],
    )
    assert exchange.fills == [FillView(seq=1, time=0, item=DATA, quantity=3, price=30)]


def test_the_exchange_marks_the_viewers_own_fills() -> None:
    world = perform(CRAFT_WORLD, PlaceOrder("a", "sell", DATA, 8, 30))
    world = perform(world, PlaceOrder("b", "buy", DATA, 3, 32))

    seen = {viewer: world_view(world, 1, viewer=viewer).exchange for viewer in ("a", "b", "c")}

    assert [seen[viewer].fills[0].own for viewer in ("a", "b", "c")] == ["sold", "bought", None]


def test_the_exchange_lists_the_viewers_orders_and_names_the_viewer() -> None:
    world = perform(CRAFT_WORLD, PlaceOrder("a", "sell", DATA, 8, 30))

    exchange = world_view(replace(world, time=100), 1, viewer="a").exchange

    assert exchange.viewer_id == "a"
    assert exchange.orders == [
        OrderView(id=1, side="sell", item=DATA, quantity=8, price=30, remaining_seconds=200)
    ]


@pytest.mark.parametrize("viewer", ["c", None], ids=["someone_else", "nobody"])
def test_the_exchange_lists_no_orders_for_another_viewer(viewer: str | None) -> None:
    world = perform(CRAFT_WORLD, PlaceOrder("a", "sell", DATA, 8, 30))

    exchange = world_view(world, 1, viewer=viewer).exchange

    assert (exchange.orders, exchange.viewer_id) == ([], viewer)


def test_fill_times_are_sent_to_a_tenth_of_a_second() -> None:
    world = perform(
        replace(CRAFT_WORLD, time=74.07406799999997), PlaceOrder("a", "sell", DATA, 8, 30)
    )
    world = perform(world, PlaceOrder("b", "buy", DATA, 3, 32))

    exchange = world_view(world, 1).exchange

    assert exchange.fills[0].time == 74.1
