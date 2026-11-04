from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.events import EventError, pending_for, send_event
from aiverse_server.core.inventory import coin_balance, list_items
from aiverse_server.core.world import (
    Inventory,
    Memory,
    Notice,
    NoticeLog,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.trade import PurchaseOffer, SaleOffer
from aiverse_server.game.actions import ActionRejected, LeaveGame, MoveTo, OfferToBuy, perform
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import (
    GameError,
    announce,
    check_can_act,
    end_game,
    leave_game,
    ranks,
    total_coins,
)
from aiverse_server.residents.walking import next_arrival


def resident(resident_id: str, coins: int, items: tuple[tuple[str, int], ...] = ()) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(1, 1),
        inventory=Inventory(items),
        coins=coins,
    )


def arrive(world: World) -> World:
    """Skip ahead until every walk has ended."""
    while (due := next_arrival(world)) is not None:
        world = settle_due(replace(world, time=due))
    return world


WORLD = World(
    map=WorldMap(size=10),
    residents=(
        resident("a", 100, (("bread", 4),)),
        resident("b", 50, (("energy", 20), ("wood", 3))),
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


B_LEFT = leave_game(WORLD, "b")


@pytest.mark.parametrize(
    "action",
    [LeaveGame("b"), MoveTo("b", Position(2, 2)), OfferToBuy("a", "b", "wood", 1, 1)],
    ids=["leave", "move", "receive an offer"],
)
def test_a_resident_who_left_can_neither_act_nor_be_dealt_with(
    action: LeaveGame | MoveTo | OfferToBuy,
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(B_LEFT, action)

    assert raised.value.code == "resident_left"


def test_a_resident_who_left_cannot_leave_again() -> None:
    with pytest.raises(GameError) as raised:
        leave_game(B_LEFT, "b")

    assert raised.value.code == "resident_left"


def test_a_resident_who_left_receives_no_events() -> None:
    with pytest.raises(EventError) as raised:
        send_event(B_LEFT, "a", "b", PurchaseOffer("wood", 1, 1))

    assert raised.value.code == "resident_left"


def test_the_game_goes_on_after_someone_leaves() -> None:
    world = perform(WORLD, LeaveGame("a"))
    world = arrive(perform(world, MoveTo("b", Position(5, 5))))
    assert world.residents[1].position == Position(5, 5)
    assert not world.ended


def test_ending_settles_every_offer() -> None:
    world = end_game(with_offers(WORLD))

    assert world.ended
    assert world.events.pending == ()
    assert world.residents == WORLD.residents


def test_nobody_acts_once_the_game_has_ended() -> None:
    world = end_game(WORLD)

    with pytest.raises(ActionRejected) as raised:
        perform(world, MoveTo("a", Position(2, 2)))

    assert raised.value.code == "game_over"


def test_ending_an_ended_game_changes_nothing() -> None:
    world = end_game(WORLD)

    assert end_game(world) is world


def test_check_can_act_reports_unknown_residents() -> None:
    with pytest.raises(GameError) as raised:
        check_can_act(WORLD, "nobody")

    assert raised.value.code == "unknown_resident"


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


def test_announcements_are_numbered_in_the_order_they_were_made() -> None:
    timed = replace(WORLD, time=30.0)

    world = announce(announce(timed, "first"), "second")

    assert world.notices.entries == (Notice(1, 30.0, "first"), Notice(2, 30.0, "second"))
    assert world.notices.next_seq == 3


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: NoticeLog((Notice(1, 0.0, "hi"),), 1), "must be an integer above 0"),
        (lambda: NoticeLog((Notice(2, 0.0, "a"), Notice(1, 0.0, "b")), 3), "above 2"),
        (lambda: NoticeLog((Notice(1, 0.0, " "),), 2), "is empty"),
        (lambda: NoticeLog((), 0), "must be an integer >= 1"),
        (lambda: Memory(notices_seen=-1), "notices seen must be an integer >= 0"),
        (
            lambda: replace(
                WORLD,
                residents=(replace(WORLD.residents[0], memory=Memory(notices_seen=1)),),
            ),
            "not yet announced",
        ),
    ],
    ids=[
        "seq not below next",
        "seq out of order",
        "empty text",
        "next seq below one",
        "negative seen",
        "read before announced",
    ],
)
def test_a_world_rejects_notices_it_cannot_account_for(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()
