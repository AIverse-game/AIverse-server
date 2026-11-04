import json
from dataclasses import replace
from typing import Any

from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.energy import ENERGY
from aiverse_server.game.actions import OfferToSell, ProposeContract, RespondToEvent, Say, perform
from aiverse_server.server.protocol import (
    ERROR_MAX_LENGTH,
    SPEECH_VIEW_LIMIT,
    bad_message,
    parse_client_message,
    world_view,
)

STOCK = Inventory(((ENERGY, 100), ("C1", 1)))

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


def test_a_crystal_set_aside_in_an_offer_still_shows_as_owned() -> None:
    world = perform(WORLD, OfferToSell("a", "b", "C1", 1, 1))
    assert resident_view(world_view(world, 1).model_dump(by_alias=True), "a")["crystals"] == {
        "C1": 1
    }


def test_states_show_only_the_viewers_offers_and_contracts() -> None:
    rich = tuple(replace(resident, coins=1000) for resident in WORLD.residents)
    world = replace(WORLD, residents=rich)
    # b offers an energy, a proposes b a contract and b signs it, then a proposes another.
    world = perform(world, OfferToSell("b", "a", ENERGY, 5, 80))
    world = perform(world, ProposeContract("a", "b", False, ENERGY, 10, 100, 3, 60, deposit=20))
    world = perform(world, RespondToEvent("b", 2, True))
    world = perform(
        replace(world, time=15), ProposeContract("b", "a", True, "C1", 1, 200, 1, 30, deposit=50)
    )
    shown = world_view(world, 100, viewer="a").model_dump(by_alias=True, exclude_none=True)
    assert shown["offers"] == [
        {
            "eventId": 1,
            "side": "sell",
            "fromId": "b",
            "toId": "a",
            "item": ENERGY,
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
            "item": "C1",
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
            "item": ENERGY,
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
