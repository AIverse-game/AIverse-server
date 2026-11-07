"""Servers, the meeting rooms they host and the fees their owners charge."""

from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance, item_count
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.facilities import (
    FACILITY_SECONDS,
    MEETING_COMPUTE,
    MEETING_MATERIALS,
    SERVER_CAPACITY,
    SERVER_MATERIALS,
    ad_income,
    meeting_income,
    next_facility_cycle,
)
from aiverse_server.economy.studio import MATERIAL
from aiverse_server.game.actions import (
    ActionRejected,
    BuildMeetingRoom,
    BuildServer,
    LeaveGame,
    SetHostingFee,
    SupplyServer,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game


def resident(resident_id: str, x: float, y: float) -> Resident:
    stacks = ((COMPUTE, 100), (MATERIAL, SERVER_MATERIALS), ("v1", 1))
    return Resident(
        resident_id, resident_id, "#123456", Position(x, y), Inventory(stacks), coins=100
    )


# a stands in their bare parcel L5-3 north of the market; b and c own L6-3 and L7-3 east of it,
# 10 and 20 units away, and d owns L9-9 in the far corner, out of reach. Each stands in their
# own parcel.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 55, 35),
        resident("b", 65, 35),
        resident("c", 75, 35),
        resident("d", 95, 95),
    ),
    market=Market(Position(50, 50), 5),
    parcels=(Parcel(5, 3, "a"), Parcel(6, 3, "b"), Parcel(7, 3, "c"), Parcel(9, 9, "d")),
)

ROOM = Facility("meeting", 0.0, FACILITY_SECONDS)


def server(fee: int, compute: int = 100) -> Facility:
    return Facility("server", 0.0, None, compute=compute, fee=fee)


def built(world: World, **facilities: Facility) -> World:
    """`world` with the facility given for each owner on their parcel."""
    return replace(
        world,
        parcels=tuple(
            replace(parcel, facility=facilities[parcel.owner_id])
            if parcel.owner_id in facilities
            else parcel
            for parcel in world.parcels
        ),
    )


def facility(world: World, owner_id: str) -> Facility:
    found = next(parcel.facility for parcel in world.parcels if parcel.owner_id == owner_id)
    assert found is not None
    return found


def pay_first_cycle(world: World) -> World:
    return settle_due(replace(world, time=FACILITY_SECONDS))


def test_a_server_is_built_of_materials_and_starts_empty_without_a_cycle() -> None:
    world = perform(replace(WORLD, time=12.0), BuildServer("a", 15))

    assert item_count(world, "a", MATERIAL) == 0
    assert facility(world, "a") == Facility("server", 12.0, None, fee=15)
    assert next_facility_cycle(world) is None


def test_a_meeting_room_uses_up_materials_and_a_v1_program() -> None:
    world = perform(WORLD, BuildMeetingRoom("a"))

    assert item_count(world, "a", MATERIAL) == SERVER_MATERIALS - MEETING_MATERIALS
    assert item_count(world, "a", "v1") == 0
    assert facility(world, "a") == ROOM


@pytest.mark.parametrize(
    ("carried", "code"),
    [
        (((COMPUTE, 100), (MATERIAL, MEETING_MATERIALS)), "not_enough_programs"),
        (((COMPUTE, 100), (MATERIAL, MEETING_MATERIALS - 1), ("v1", 1)), "not_enough_materials"),
    ],
)
def test_a_meeting_room_without_its_materials_or_program_is_refused(
    carried: tuple[tuple[str, int], ...], code: str
) -> None:
    a = replace(WORLD.residents[0], inventory=Inventory(carried))
    world = replace(WORLD, residents=(a, *WORLD.residents[1:]))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuildMeetingRoom("a"))

    assert rejected.value.code == code


def test_anyone_standing_on_a_server_can_supply_it_compute() -> None:
    world = built(WORLD, b=server(10, compute=0))
    visitor = replace(world.residents[0], position=Position(65, 35))
    world = replace(world, residents=(visitor, *world.residents[1:]))

    supplied = perform(world, SupplyServer("a", 40))

    assert facility(supplied, "b").compute == 40
    assert item_count(supplied, "a", COMPUTE) == 60


@pytest.mark.parametrize(
    ("world", "amount", "code"),
    [
        (built(WORLD, a=server(0, compute=SERVER_CAPACITY - 5)), 6, "server_full"),
        (built(WORLD, a=server(0)), 0, "invalid_amount"),
        (built(WORLD, a=ROOM), 5, "no_server"),
        (WORLD, 5, "no_server"),
    ],
)
def test_supplying_a_server_that_cannot_take_it_is_refused(
    world: World, amount: int, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(world, SupplyServer("a", amount))

    assert rejected.value.code == code


def test_a_server_whose_owner_left_takes_no_more_compute() -> None:
    world = perform(built(WORLD, b=server(10)), LeaveGame("b"))
    visitor = replace(world.residents[0], position=Position(65, 35))
    world = replace(world, residents=(visitor, *world.residents[1:]))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, SupplyServer("a", 5))

    assert rejected.value.code == "server_closed"


def test_a_hosted_meeting_room_pays_its_owner_less_the_fee_the_server_takes() -> None:
    world = pay_first_cycle(built(WORLD, a=ROOM, b=server(15)))

    # 102.5 visitors at 30s, 2 coins each, rounded down.
    assert coin_balance(world, "a") == 100 + 204 - 15
    assert coin_balance(world, "b") == 100 + 15
    room, host = facility(world, "a"), facility(world, "b")
    assert (room.last_income, room.earned, room.host) == (189, 189, "L6-3")
    assert (host.compute, host.earned) == (100 - MEETING_COMPUTE, 15)
    paid = [(t.payer, t.payee, t.amount, t.channel) for t in world.ledger.transfers[-2:]]
    assert paid == [(SYSTEM, "a", 204, "visitors"), ("a", "b", 15, "hosting")]


def test_a_room_uses_its_owners_own_server_free_before_a_cheaper_one() -> None:
    world = built(WORLD, a=ROOM, b=server(0))
    world = replace(world, parcels=(*world.parcels, Parcel(5, 2, "a", server(50))))

    paid = pay_first_cycle(world)

    assert (facility(paid, "a").host, facility(paid, "a").last_income) == ("L5-2", 204)


def test_a_room_picks_the_cheapest_server_in_reach_with_enough_compute() -> None:
    world = built(
        WORLD,
        a=ROOM,
        b=server(20),
        c=server(5, compute=MEETING_COMPUTE - 1),
        d=server(0),
    )

    paid = pay_first_cycle(world)

    # c is cheaper but nearly empty; d is free but 64 units away.
    assert facility(paid, "a").host == "L6-3"


def test_a_room_no_server_can_host_earns_nothing_that_cycle() -> None:
    world = pay_first_cycle(built(WORLD, a=ROOM, b=server(0, compute=0)))

    room = facility(world, "a")
    assert (room.last_income, room.host, room.next_at) == (0, None, 2 * FACILITY_SECONDS)
    assert coin_balance(world, "a") == 100


def test_the_fee_never_takes_more_than_the_rooms_income() -> None:
    world = pay_first_cycle(built(WORLD, a=ROOM, b=server(1_000)))

    assert facility(world, "a").last_income == 0
    assert coin_balance(world, "b") == 100 + 204


def test_a_server_whose_owner_left_hosts_no_more() -> None:
    world = perform(built(WORLD, a=ROOM, b=server(0)), LeaveGame("b"))

    paid = pay_first_cycle(world)

    assert facility(paid, "a").host is None
    assert facility(paid, "b").compute == 100


def test_meeting_rooms_near_each_other_split_the_visitors() -> None:
    world = replace(built(WORLD, a=ROOM, c=ROOM), time=FACILITY_SECONDS)

    # a and c are 20 units apart; the far corner is more than 40 units from both.
    assert meeting_income(world, 5, 3) == 102
    assert meeting_income(world, 9, 9) == 204


def test_ads_share_footfall_only_with_other_ads() -> None:
    ad = Facility("ad", 0.0, FACILITY_SECONDS)
    alone = replace(built(WORLD, a=ad), time=FACILITY_SECONDS)
    beside = replace(built(WORLD, a=ad, b=server(0), c=ROOM), time=FACILITY_SECONDS)

    assert ad_income(beside, 5, 3) == ad_income(alone, 5, 3)


def test_the_owner_changes_a_servers_fee_from_another_parcel() -> None:
    world = built(WORLD, b=server(10))
    away = replace(world.residents[1], position=Position(95, 5))
    world = replace(world, residents=(world.residents[0], away, *world.residents[2:]))

    changed = perform(world, SetHostingFee("b", "L6-3", 30))

    assert facility(changed, "b").fee == 30


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (SetHostingFee("a", "L6-3", 0), "no_server"),
        (SetHostingFee("c", "L7-3", 0), "no_server"),
        (SetHostingFee("b", "L6-3", -1), "invalid_fee"),
        (BuildServer("a", -1), "invalid_fee"),
    ],
)
def test_a_fee_that_cannot_be_set_is_refused(
    action: SetHostingFee | BuildServer, code: str
) -> None:
    world = built(WORLD, b=server(10))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, action)

    assert rejected.value.code == code


@pytest.mark.parametrize(
    ("world", "action", "code"),
    [
        (built(WORLD, a=ROOM), BuildServer("a", 0), "parcel_built"),
        (built(WORLD, a=server(0)), BuildMeetingRoom("a"), "parcel_built"),
        (replace(WORLD, parcels=WORLD.parcels[1:]), BuildServer("a", 0), "not_your_parcel"),
        (replace(WORLD, parcels=WORLD.parcels[1:]), BuildMeetingRoom("a"), "not_your_parcel"),
    ],
)
def test_servers_and_meeting_rooms_go_only_on_ones_own_bare_parcel(
    world: World, action: BuildServer | BuildMeetingRoom, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(world, action)

    assert rejected.value.code == code


def test_a_server_can_be_filled_to_capacity() -> None:
    world = built(WORLD, a=server(0, compute=SERVER_CAPACITY - 5))

    supplied = perform(world, SupplyServer("a", 5))

    assert facility(supplied, "a").compute == SERVER_CAPACITY


def test_of_equally_cheap_servers_a_room_uses_the_nearest() -> None:
    world = built(WORLD, b=ROOM, a=server(5), c=server(5))
    world = replace(world, parcels=(*world.parcels, Parcel(4, 3, "d", server(5))))

    paid = pay_first_cycle(world)

    # a's L5-3 and c's L7-3 are both 10 units from b's room; L5-3 comes first row by row and
    # column by column, d's L4-3 is 20 units away.
    assert facility(paid, "b").host == "L5-3"


def test_of_its_owners_own_servers_a_room_uses_the_nearest_whatever_their_fees() -> None:
    world = built(WORLD, a=ROOM)
    world = replace(
        world,
        parcels=(*world.parcels, Parcel(5, 2, "a", server(50)), Parcel(5, 1, "a", server(0))),
    )

    paid = pay_first_cycle(world)

    assert facility(paid, "a").host == "L5-2"


def test_rooms_due_together_draw_on_a_short_server_in_the_order_their_land_was_bought() -> None:
    world = pay_first_cycle(built(WORLD, a=ROOM, c=ROOM, b=server(5, compute=MEETING_COMPUTE)))

    # Each of the two rooms 20 units apart earns half of 204; only a's, bought first, is hosted.
    assert [facility(world, "a").last_income, facility(world, "c").last_income] == [97, 0]
    assert facility(world, "b").compute == 0


def test_a_room_whose_owner_left_neither_earns_nor_draws_compute_nor_shares() -> None:
    world = perform(built(WORLD, a=ROOM, c=ROOM, b=server(0)), LeaveGame("a"))

    paid = pay_first_cycle(world)

    assert facility(paid, "a").last_income == 0
    assert facility(paid, "c").last_income == 204
    assert facility(paid, "b").compute == 100 - MEETING_COMPUTE


def test_rooms_neither_earn_nor_draw_compute_once_the_game_is_over() -> None:
    ended = end_game(built(WORLD, a=ROOM, b=server(0)))

    world = pay_first_cycle(ended)

    assert coin_balance(world, "a") == 100
    assert facility(world, "b").compute == 100
