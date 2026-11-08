from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import PARCEL_SHARES
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.land import (
    block_parcels,
    grid_size,
    parcel_centre,
    plot,
    plot_at,
    plots,
)
from aiverse_server.game.actions import (
    ActionRejected,
    BuyParcel,
    OfferJointPurchase,
    OfferShares,
    RespondToEvent,
    perform,
)
from aiverse_server.game.layout import MAP_SIZE, create_default_world


def resident(resident_id: str, x: float, y: float, coins: int = 20_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(x, y), coins=coins)


# Three blocks of 3 x 3 parcels side by side, 90 across. The block in the middle (30 to 60) is
# the lobby. a stands in the first block, b in the lobby.
MAP = WorldMap(size=90, block_cells=3, block_price_factor=5, lobby=True)
WORLD = World(map=MAP, residents=(resident("a", 20, 20), resident("b", 45, 45)))


def test_blocks_touch_each_other_with_no_road_between() -> None:
    assert grid_size(WORLD) == 9
    assert parcel_centre(MAP, 2, 0) == Position(25, 5)
    assert parcel_centre(MAP, 3, 0) == Position(35, 5)
    assert plot_at(WORLD, Position(30, 5)).id == "L3-0"


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
    single = WorldMap(size=90, block_cells=3)

    block = plot(WORLD, 0, 0).price
    one = plot(World(map=single, residents=()), 0, 0).price

    assert block == 5 * one


def test_the_lobby_cannot_be_bought() -> None:
    with pytest.raises(ActionRejected) as error:
        perform(replace(WORLD, residents=(resident("b", 45, 45),)), BuyParcel("b"))

    assert error.value.code == "not_for_sale"


def test_a_block_someone_owns_cannot_be_bought_again() -> None:
    world = perform(WORLD, BuyParcel("a"))
    world = replace(world, residents=(*world.residents[:1], resident("c", 25, 25)))

    with pytest.raises(ActionRejected) as error:
        perform(world, BuyParcel("c"))

    assert error.value.code == "parcel_owned"


def test_the_default_world_has_five_blocks_a_side_around_a_lobby() -> None:
    world = create_default_world()

    assert world.map.size == MAP_SIZE == 150
    assert grid_size(world) == 15
    assert sum(square.lobby for square in plots(world)) == 9
    assert plot_at(world, world.residents[0].position).lobby


def test_a_joint_purchase_gives_the_whole_block_to_both() -> None:
    world = replace(WORLD, residents=(*WORLD.residents, resident("c", 80, 80)))
    world = perform(world, OfferJointPurchase("a", "c", 3_000, 40))
    event = world.events.pending[0]

    world = perform(world, RespondToEvent("c", event.id, True))

    assert len(world.parcels) == 9
    assert {parcel.owner_id for parcel in world.parcels} == {"a"}
    assert all(parcel.shares == (("a", 60), ("c", 40)) for parcel in world.parcels)


def test_a_joint_purchase_cannot_be_proposed_from_the_lobby() -> None:
    world = replace(WORLD, residents=(resident("b", 45, 45), resident("c", 20, 20)))

    with pytest.raises(ActionRejected) as error:
        perform(world, OfferJointPurchase("b", "c", 3_000, 40))

    assert error.value.code == "not_for_sale"


def test_shares_change_hands_for_the_whole_block_whichever_parcel_is_named() -> None:
    bought = perform(WORLD, BuyParcel("a"))
    world = replace(bought, residents=(*bought.residents, resident("c", 80, 80)))

    offered = perform(world, OfferShares("a", "c", "L1-2", 30, 500))
    event = offered.events.pending[0]
    world = perform(offered, RespondToEvent("c", event.id, True))

    assert len(block_parcels(world, "L0-0")) == 9
    assert all(parcel.shares == (("a", 70), ("c", 30)) for parcel in world.parcels)
    assert coin_balance(world, "c") == 20_000 - 500


def test_shares_promised_in_one_parcel_of_a_block_count_against_all_its_parcels() -> None:
    bought = perform(WORLD, BuyParcel("a"))
    world = replace(bought, residents=(*bought.residents, resident("c", 80, 80)))
    world = perform(world, OfferShares("a", "c", "L0-0", 80, 0))

    with pytest.raises(ActionRejected) as error:
        perform(world, OfferShares("a", "c", "L2-2", 30, 0))

    assert error.value.code == "not_enough_shares"


@pytest.mark.parametrize("alias", ["L01-0", "L٣-1", "l0-0", "L0-0 ", "L" + "9" * 5000 + "-1"])
def test_a_share_offer_must_name_a_parcel_by_its_exact_id(alias: str) -> None:
    bought = perform(WORLD, BuyParcel("a"))
    world = replace(bought, residents=(*bought.residents, resident("c", 80, 80)))

    with pytest.raises(ActionRejected) as error:
        perform(world, OfferShares("a", "c", alias, 30, 0))

    assert error.value.code == "unknown_parcel"
