from dataclasses import replace

import pytest

from aiverse_server.actions import ActionRejected, LeaveGame, MoveTo, OfferToBuy, perform
from aiverse_server.events import EventError, pending_for, send_event
from aiverse_server.inventory import coin_balance, list_items
from aiverse_server.lifecycle import (
    GameError,
    check_can_act,
    end_game,
    leave_game,
    ranks,
    total_coins,
)
from aiverse_server.trade import PurchaseOffer, SaleOffer
from aiverse_server.world import Inventory, Position, Resident, World, WorldMap


def resident(resident_id: str, coins: int, items: tuple[tuple[str, int], ...] = ()) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(1, 1),
        inventory=Inventory(items),
        coins=coins,
    )


WORLD = World(
    map=WorldMap(size=10),
    residents=(
        resident("a", 100, (("bread", 4),)),
        resident("b", 50, (("wood", 3),)),
        resident("c", 50),
    ),
)


def with_offers(world: World) -> World:
    """a buys from b, b sells to a, and c buys from b: every reservation kind in play."""
    world, _ = send_event(world, "a", "b", PurchaseOffer("wood", 1, 30))
    world, _ = send_event(world, "b", "a", SaleOffer("wood", 2, 10))
    world, _ = send_event(world, "c", "b", PurchaseOffer("wood", 1, 20))
    return world


def test_leaving_settles_their_offers_clears_items_and_keeps_coins() -> None:
    world = leave_game(with_offers(WORLD), "b")
    b = world.residents[1]
    assert b.left
    assert b.coins == 50
    assert list_items(world, "b") == {}
    # Everyone b dealt with got back what their offers set aside.
    assert coin_balance(world, "a") == 100
    assert coin_balance(world, "c") == 50
    assert list_items(world, "a") == {"bread": 4}
    assert world.events.pending == ()


def test_leaving_keeps_offers_between_others() -> None:
    world, offer = send_event(WORLD, "a", "c", PurchaseOffer("bread", 1, 5))
    world = leave_game(world, "b")
    assert pending_for(world, "c") == (offer,)


def test_a_resident_who_left_can_neither_act_nor_be_dealt_with() -> None:
    world = leave_game(WORLD, "b")
    with pytest.raises(GameError) as raised:
        leave_game(world, "b")
    assert raised.value.code == "resident_left"
    with pytest.raises(EventError) as sent:
        send_event(world, "a", "b", PurchaseOffer("wood", 1, 1))
    assert sent.value.code == "resident_left"
    actions: list[LeaveGame | MoveTo] = [LeaveGame("b"), MoveTo("b", Position(2, 2))]
    for action in actions:
        with pytest.raises(ActionRejected) as rejected:
            perform(world, action)
        assert rejected.value.code == "resident_left"
    rejected_offer = OfferToBuy("a", "b", "wood", 1, 1)
    with pytest.raises(ActionRejected) as rejected:
        perform(world, rejected_offer)
    assert rejected.value.code == "resident_left"


def test_the_game_goes_on_after_someone_leaves() -> None:
    world = perform(WORLD, LeaveGame("a"))
    world = perform(world, MoveTo("b", Position(5, 5)))
    assert world.residents[1].position == Position(5, 5)
    assert not world.ended


def test_ending_settles_every_offer_and_freezes_the_world() -> None:
    world = end_game(with_offers(WORLD))
    assert world.ended
    assert world.events.pending == ()
    assert world.residents == WORLD.residents
    assert end_game(world) is world
    with pytest.raises(ActionRejected) as raised:
        perform(world, MoveTo("a", Position(2, 2)))
    assert raised.value.code == "game_over"


def test_check_can_act_reports_unknown_residents() -> None:
    with pytest.raises(GameError) as raised:
        check_can_act(WORLD, "nobody")
    assert raised.value.code == "unknown_resident"
    check_can_act(WORLD, "a")


def test_ranks_count_reserved_coins_and_share_places_on_ties() -> None:
    assert ranks(WORLD) == {"a": 1, "b": 2, "c": 2}
    # c sets 20 aside: still 50 in total, so the ranking does not move.
    world, _ = send_event(WORLD, "c", "b", PurchaseOffer("wood", 1, 20))
    assert coin_balance(world, "c") == 30
    assert total_coins(world, "c") == 50
    assert ranks(world) == {"a": 1, "b": 2, "c": 2}


def test_residents_who_left_keep_their_place() -> None:
    richer = replace(WORLD, residents=(*WORLD.residents[:2], resident("c", 500)))
    world = leave_game(richer, "c")
    assert ranks(world) == {"c": 1, "a": 2, "b": 3}
