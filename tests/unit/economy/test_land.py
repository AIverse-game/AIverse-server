from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    Resident,
    Spring,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.land import grid_size, plot, plot_at, plots
from aiverse_server.game.actions import ActionRejected, BuyParcel, LeaveGame, perform
from aiverse_server.game.layout import create_default_world


def resident(resident_id: str, x: float, y: float, coins: int = 5_000) -> Resident:
    return Resident(
        resident_id,
        resident_id,
        "#123456",
        Position(x, y),
        Inventory(((COMPUTE, 100),)),
        coins=coins,
    )


# A 10 x 10 grid of parcels. a stands in L5-3, b in L0-0 with little money, c on the spring.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 55, 35),
        resident("b", 5, 5, coins=1_000),
        resident("c", 15, 15),
    ),
    market=Market(Position(50, 50), 5),
    springs=(Spring("s1", Position(15, 15), 4, 100),),
)


def test_the_grid_reserves_every_parcel_touching_a_shared_place() -> None:
    reserved = [square.id for square in plots(WORLD) if square.reserved]

    assert grid_size(WORLD) == 10
    assert reserved == ["L1-1", "L4-4", "L5-4", "L4-5", "L5-5"]


def test_land_costs_more_nearer_the_market() -> None:
    assert [plot(WORLD, 0, 0).price, plot(WORLD, 5, 3).price, plot(WORLD, 9, 9).price] == [
        1_130,
        2_540,
        1_130,
    ]


def test_the_last_column_and_row_reach_the_map_edge() -> None:
    assert plot_at(WORLD, Position(100, 100)).id == "L9-9"


def test_buying_a_parcel_pays_the_town_and_makes_it_the_buyers() -> None:
    world = perform(WORLD, BuyParcel("a"))

    assert world.parcels == (Parcel(5, 3, "a"),)
    assert coin_balance(world, "a") == 5_000 - 2_540
    last = world.ledger.transfers[-1]
    assert (last.payer, last.payee, last.amount, last.channel) == ("a", SYSTEM, 2_540, "land")


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (WORLD, BuyParcel("c"), "parcel_reserved"),
        (replace(WORLD, parcels=(Parcel(5, 3, "b"),)), BuyParcel("a"), "parcel_owned"),
        (WORLD, BuyParcel("b"), "not_enough_coins"),
    ],
)
def test_a_parcel_that_cannot_be_bought_is_refused(
    world: World, action: BuyParcel, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(world, action)

    assert rejected.value.code == code


def test_land_stays_its_owners_after_they_leave() -> None:
    world = perform(perform(WORLD, BuyParcel("a")), LeaveGame("a"))

    assert world.parcels == (Parcel(5, 3, "a"),)
    assert plot(world, 5, 3).owner_id == "a"


def test_the_default_map_leaves_most_land_free_to_buy() -> None:
    squares = plots(create_default_world(1))

    assert len(squares) == 225
    assert 150 < sum(not square.reserved for square in squares) < 225
