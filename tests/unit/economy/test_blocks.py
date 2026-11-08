from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import PARCEL_SHARES
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.land import grid_size, on_road, parcel_centre, plot, plot_at, plots
from aiverse_server.game.actions import (
    ActionRejected,
    BuyParcel,
    OfferJointPurchase,
    RespondToEvent,
    perform,
)
from aiverse_server.game.layout import MAP_SIZE, create_default_world


def resident(resident_id: str, x: float, y: float, coins: int = 20_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(x, y), coins=coins)


# Three blocks of 3 x 3 parcels, with roads of 5 around them: 3 * 30 + 4 * 5 = 110. The block
# in the middle is the lobby. Block columns start at 5, 40 and 75.
MAP = WorldMap(size=110, block_cells=3, road=5, block_price_factor=5, lobby=True)
WORLD = World(map=MAP, residents=(resident("a", 20, 20), resident("b", 55, 55)))


def test_parcels_lie_in_blocks_with_roads_between() -> None:
    assert grid_size(WORLD) == 9
    assert parcel_centre(MAP, 0, 0) == Position(10, 10)
    assert parcel_centre(MAP, 2, 0) == Position(30, 10)
    assert parcel_centre(MAP, 3, 0) == Position(45, 10)


def test_a_position_on_a_road_belongs_to_no_parcel_but_has_a_nearest_one() -> None:
    assert on_road(WORLD, Position(37, 20)) and on_road(WORLD, Position(2, 2))
    assert not on_road(WORLD, Position(20, 20))
    assert plot_at(WORLD, Position(37, 20)).id == "L2-1"


def test_every_parcel_of_a_block_lists_the_blocks_price() -> None:
    assert len({plot(WORLD, col, row).price for col in range(3) for row in range(3)}) == 1
    assert plot(WORLD, 0, 0).price == plot(WORLD, 2, 2).price != plot(WORLD, 3, 3).price


def test_the_middle_block_is_the_lobby() -> None:
    assert {square.id for square in plots(WORLD) if square.lobby} == {
        f"L{col}-{row}" for col in range(3, 6) for row in range(3, 6)
    }


def test_buying_gives_the_whole_block_to_the_buyer() -> None:
    price = plot_at(WORLD, Position(20, 20)).price

    world = perform(WORLD, BuyParcel("a"))

    assert {parcel.id for parcel in world.parcels} == {
        f"L{col}-{row}" for col in range(3) for row in range(3)
    }
    assert {parcel.owner_id for parcel in world.parcels} == {"a"}
    assert all(parcel.shares == (("a", PARCEL_SHARES),) for parcel in world.parcels)
    assert coin_balance(world, "a") == 20_000 - price


def test_a_block_costs_five_times_a_single_parcel_there() -> None:
    single = WorldMap(size=110, block_cells=3, road=5)

    block = plot(WORLD, 0, 0).price
    one = plot(World(map=single, residents=()), 0, 0).price

    assert block == 5 * one


@pytest.mark.parametrize("name", ["lobby", "road"])
def test_the_lobby_and_the_roads_cannot_be_bought(name: str) -> None:
    where = {"lobby": Position(55, 55), "road": Position(37, 20)}[name]
    world = replace(WORLD, residents=(resident("a", where.x, where.y),))

    with pytest.raises(ActionRejected) as error:
        perform(world, BuyParcel("a"))

    assert error.value.code == "not_for_sale"


def test_a_block_someone_owns_cannot_be_bought_again() -> None:
    world = perform(WORLD, BuyParcel("a"))
    world = replace(world, residents=(*world.residents[:1], resident("b", 25, 25)))

    with pytest.raises(ActionRejected) as error:
        perform(world, BuyParcel("b"))

    assert error.value.code == "parcel_owned"


def test_the_default_world_has_five_blocks_a_side_around_a_lobby() -> None:
    world = create_default_world()

    assert world.map.size == MAP_SIZE == 186
    assert grid_size(world) == 15
    assert sum(square.lobby for square in plots(world)) == 9
    assert plot_at(world, world.residents[0].position).lobby


def test_a_joint_purchase_gives_the_whole_block_to_both() -> None:
    world = perform(WORLD, OfferJointPurchase("a", "b", 3_000, 40))
    event = world.events.pending[0]

    world = perform(world, RespondToEvent("b", event.id, True))

    assert len(world.parcels) == 9
    assert {parcel.owner_id for parcel in world.parcels} == {"a"}
    assert all(parcel.shares == (("a", 60), ("b", 40)) for parcel in world.parcels)


def test_a_joint_purchase_cannot_be_proposed_from_a_road_or_the_lobby() -> None:
    for x, y in ((37, 20), (55, 55)):
        world = replace(WORLD, residents=(resident("a", x, y), resident("b", 20, 20)))

        with pytest.raises(ActionRejected) as error:
            perform(world, OfferJointPurchase("a", "b", 3_000, 40))

        assert error.value.code == "not_for_sale"
