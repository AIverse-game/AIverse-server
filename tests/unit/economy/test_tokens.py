from dataclasses import replace

import pytest

from aiverse_server.core.events import pending_from
from aiverse_server.core.inventory import coin_balance, item_count, list_items
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN, token_total, walk_cost
from aiverse_server.game.actions import (
    ActionRejected,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    RespondToEvent,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.residents.walking import next_arrival


def resident(resident_id: str, x: float, tokens: int, coins: int = 0) -> Resident:
    items = ((TOKEN, tokens),) if tokens else ()
    return Resident(
        id=resident_id,
        name=resident_id,
        color="#123456",
        position=Position(x, 0),
        inventory=Inventory(items),
        coins=coins,
    )


# a walks and sells tokens; b buys them; c has none to begin with.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 0, 10, coins=5),
        resident("b", 50, 100, coins=100),
        resident("c", 90, 0),
    ),
)


def arrive(world: World) -> World:
    """Skip ahead until every walk has ended."""
    while (due := next_arrival(world)) is not None:
        world = settle_due(replace(world, time=due))
    return world


@pytest.mark.parametrize(
    ("end", "cost"),
    [(Position(0, 0), 0), (Position(3, 4), 3), (Position(0, 0.01), 1), (Position(2.5, 0), 2)],
)
def test_a_walk_costs_its_distance_rounded_up(end: Position, cost: int) -> None:
    assert walk_cost(Position(0, 0), end) == cost


def test_walking_spends_tokens_when_setting_off() -> None:
    walking = perform(WORLD, MoveTo("a", Position(3, 4)))
    arrived = arrive(walking)

    assert item_count(walking, "a", TOKEN) == 7
    assert arrived.residents[0].position == Position(3, 4)
    assert item_count(arrived, "a", TOKEN) == 7


def test_a_walk_the_resident_cannot_pay_for_is_refused() -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, MoveTo("a", Position(21, 0)))
    assert raised.value.code == "not_enough_tokens"


def test_standing_still_is_free_even_without_tokens() -> None:
    world = perform(WORLD, MoveTo("c", Position(90, 0)))
    assert not world.residents[2].left


def test_spending_the_last_token_on_a_walk_takes_the_resident_out_on_arrival() -> None:
    offered = perform(WORLD, OfferToBuy("a", "b", "bread", 1, 5))

    walking = perform(offered, MoveTo("a", Position(20, 0)))
    arrived = arrive(walking)

    assert not walking.residents[0].left
    a = arrived.residents[0]
    assert a.left
    assert a.position == Position(20, 0)
    # Leaving settles their offers: the coins set aside come back and stay.
    assert arrived.events.pending == ()
    assert a.coins == 5


def test_tokens_trade_like_any_item() -> None:
    offered = perform(WORLD, OfferToBuy("b", "a", TOKEN, 4, 20))

    world = perform(offered, RespondToEvent("a", 1, accept=True))

    assert (item_count(world, "a", TOKEN), coin_balance(world, "a")) == (6, 25)
    assert (item_count(world, "b", TOKEN), coin_balance(world, "b")) == (104, 80)


def test_a_buyer_short_of_coins_cannot_accept_a_token_offer() -> None:
    world = perform(WORLD, OfferToSell("b", "c", TOKEN, 30, 1))

    with pytest.raises(ActionRejected) as raised:
        perform(world, RespondToEvent("c", 1, accept=True))

    assert raised.value.code == "not_enough_coins"


def test_tokens_set_aside_in_an_offer_are_still_owned() -> None:
    world = perform(WORLD, OfferToSell("a", "b", TOKEN, 10, 40))

    withdrawn = perform(world, WithdrawEvent("a", 1))

    # Set aside, the tokens are still a's: offering them all is not running out.
    assert item_count(world, "a", TOKEN) == 0
    assert token_total(world, "a") == 10
    assert not world.residents[0].left
    assert item_count(withdrawn, "a", TOKEN) == 10


def test_tokens_set_aside_in_an_offer_cannot_pay_for_a_walk() -> None:
    world = perform(WORLD, OfferToSell("a", "b", TOKEN, 10, 40))

    with pytest.raises(ActionRejected) as raised:
        perform(world, MoveTo("a", Position(1, 0)))

    assert raised.value.code == "not_enough_tokens"


def test_selling_every_token_takes_the_seller_out_once_the_sale_completes() -> None:
    world = perform(WORLD, OfferToSell("a", "b", TOKEN, 10, 40))

    sold = perform(world, RespondToEvent("b", 1, accept=True))

    assert sold.residents[0].left
    assert sold.residents[0].coins == 45
    assert item_count(sold, "b", TOKEN) == 110


def test_handing_over_every_token_on_request_takes_the_seller_out() -> None:
    world = perform(WORLD, OfferToBuy("b", "a", TOKEN, 10, 40))
    world = perform(world, RespondToEvent("a", 1, accept=True))
    assert world.residents[0].left
    assert list_items(world, "a") == {}
    assert pending_from(world, "a") == ()


def test_residents_who_never_had_tokens_are_not_taken_out() -> None:
    world = perform(WORLD, MoveTo("b", Position(60, 0)))
    assert not world.residents[2].left
