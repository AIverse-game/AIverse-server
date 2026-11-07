"""Buying land, making materials and building ad slots through the agent tools, and what the
observation tells a model about land."""

from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.tool_selection import last_request_tool_names, selected_tool_names
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    Resident,
    Studio,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.economy.facilities import (
    AD_MATERIALS,
    FACILITY_SECONDS,
    MEETING_COMPUTE,
    MEETING_MATERIALS,
    SERVER_CAPACITY,
)
from aiverse_server.economy.gathering import DATA
from aiverse_server.economy.studio import MATERIAL
from aiverse_server.game.actions import LeaveGame, perform
from aiverse_server.server.app import Game

# a stands in the free parcel L5-3 north of the market with coins and materials for an ad;
# b stands at the studio with data for a batch of materials.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident(
            "a",
            "甲",
            "#112233",
            Position(55, 35),
            Inventory(((COMPUTE, 100), (MATERIAL, AD_MATERIALS))),
            coins=5_000,
        ),
        Resident(
            "b",
            "乙",
            "#445566",
            Position(20, 80),
            Inventory(((COMPUTE, 100), (DATA, 10))),
            coins=10,
        ),
    ),
    market=Market(Position(50, 50), 5),
    studio=Studio(Position(20, 80), 5),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# a owns their bare parcel L5-3 and carries what a meeting room takes; b runs a server on L6-3
# next to it, charging 10 coins a cycle, and stands on it.
HOSTED = replace(
    WORLD,
    residents=(
        replace(
            WORLD.residents[0],
            inventory=Inventory(((COMPUTE, 100), (MATERIAL, MEETING_MATERIALS), ("v1", 1))),
        ),
        replace(WORLD.residents[1], position=Position(65, 35)),
    ),
    parcels=(
        Parcel(5, 3, "a"),
        Parcel(6, 3, "b", Facility("server", 0.0, None, compute=100, fee=10)),
    ),
)


def make_game(world: World = WORLD) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, 600, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_the_observation_prices_the_parcel_underfoot_and_what_an_ad_there_would_earn() -> None:
    game, _ = make_game()

    seen = look(game, "a")

    here = seen.land.here
    assert here is not None
    assert (here.id, here.price, here.owner, here.facility) == ("L5-3", 2_540, None, None)
    # 84.2 visitors this far out plus a standing there, at 2 coins each.
    assert here.ad_income == 188
    assert seen.me.materials == AD_MATERIALS


def test_the_best_free_parcels_come_first_and_leave_out_reserved_land() -> None:
    game, _ = make_game()

    best = look(game, "b").land.best_free

    incomes = [parcel.ad_income or 0 for parcel in best]
    assert len(best) == 5
    assert incomes == sorted(incomes, reverse=True)
    assert not {"L4-4", "L4-5", "L5-4", "L5-5", "L1-7", "L1-8", "L2-7", "L2-8"} & {
        parcel.id for parcel in best
    }


def test_a_resident_buys_land_and_builds_an_ad_that_then_pays_them() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "a")

    bought = tools.buy_parcel()
    built = tools.build_ad()
    clock.now = FACILITY_SECONDS
    game.tick()
    seen = look(game, "a")

    assert (bought.ok, bought.message) == (True, "bought parcel L5-3")
    assert built.ok and built.message.startswith("built an ad slot on L5-3")
    (mine,) = seen.land.mine
    # Visitors have grown to 102.5 by the first payout.
    assert (mine.id, mine.facility, mine.last_income, mine.earned) == ("L5-3", "ad", 192, 192)
    assert seen.me.coins == 5_000 - 2_540 + 192


def test_make_material_starts_a_job_shown_in_the_status() -> None:
    game, _ = make_game()

    result = ResidentTools(game, "b").make_material()

    assert result.ok
    assert result.status is not None and result.status["writing_program"] == MATERIAL


def test_land_tools_are_offered_where_they_can_be_used() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    before = selected_tool_names(look(game, "a"))
    tools.buy_parcel()
    after = selected_tool_names(look(game, "a"))
    at_studio = selected_tool_names(look(game, "b"))

    assert "buy_parcel" in before and "build_ad" not in before
    assert "build_ad" in after and "buy_parcel" not in after
    assert {"make_material", "buy_parcel"} & at_studio == {"make_material"}


def test_a_resident_builds_a_meeting_room_that_a_neighbours_server_hosts() -> None:
    game, clock = make_game(HOSTED)
    tools = ResidentTools(game, "a")

    before = look(game, "a").land
    built = tools.build_meeting_room()
    clock.now = FACILITY_SECONDS
    game.tick()
    seen = look(game, "a")

    here = before.here
    assert here is not None
    assert (here.meeting_income, here.host, here.host_fee) == (200, "L6-3", 10)
    assert [(server.id, server.owner, server.fee) for server in before.servers] == [
        ("L6-3", "b", 10)
    ]
    assert built.message == (
        "built a meeting room on L5-3; it would earn 200 coins a cycle now, hosted by server "
        "L6-3 for 10"
    )
    (room,) = seen.land.mine
    assert (room.facility, room.host, room.last_income, room.ad_income) == (
        "meeting",
        "L6-3",
        204 - 10,
        None,
    )


def test_the_server_owner_sees_its_compute_and_fee() -> None:
    game, clock = make_game(HOSTED)
    ResidentTools(game, "a").build_meeting_room()
    clock.now = FACILITY_SECONDS
    game.tick()

    (server,) = look(game, "b").land.mine

    assert (server.facility, server.compute, server.fee, server.earned) == (
        "server",
        100 - MEETING_COMPUTE,
        10,
        10,
    )
    assert server.next_payout_in is None


def test_supplying_and_pricing_a_server_through_the_tools() -> None:
    game, _ = make_game(HOSTED)
    tools = ResidentTools(game, "b")

    supplied = tools.supply_server(50)
    priced = tools.set_hosting_fee("L6-3", 25)

    assert supplied.message == "server L6-3 now holds 150 compute"
    assert priced.message == "server L6-3 now charges 25 coins a cycle"


def test_server_and_meeting_room_tools_are_offered_where_they_can_be_used() -> None:
    game, _ = make_game(HOSTED)

    owner = selected_tool_names(look(game, "a"))
    server_owner = selected_tool_names(look(game, "b"))

    assert "build_meeting_room" in owner and "build_server" not in owner
    assert {"supply_server", "set_hosting_fee"} <= server_owner


def test_on_anothers_meeting_room_the_observation_shows_who_hosts_it_for_its_owner() -> None:
    # b now owns a meeting room on L6-3 hosted by a's server next door for 10, and a's own
    # second server stands on L5-2; a stands in b's parcel.
    world = replace(
        HOSTED,
        residents=(
            replace(HOSTED.residents[0], position=Position(65, 35)),
            HOSTED.residents[1],
        ),
        parcels=(
            Parcel(5, 3, "a", Facility("server", 0.0, None, compute=100, fee=10)),
            Parcel(5, 2, "a", Facility("server", 0.0, None, compute=100, fee=50)),
            Parcel(6, 3, "b", Facility("meeting", 0.0, FACILITY_SECONDS)),
        ),
    )
    game, _ = make_game(world)

    here = look(game, "a").land.here

    assert here is not None
    assert (here.facility, here.host, here.host_fee) == ("meeting", "L5-3", 10)


def test_a_full_server_or_one_whose_owner_left_is_not_offered_for_supply() -> None:
    full = replace(
        HOSTED,
        parcels=(
            HOSTED.parcels[0],
            Parcel(6, 3, "b", Facility("server", 0.0, None, compute=SERVER_CAPACITY)),
        ),
    )
    left = perform(
        replace(
            HOSTED,
            residents=(
                replace(HOSTED.residents[0], position=Position(65, 35)),
                HOSTED.residents[1],
            ),
        ),
        LeaveGame("b"),
    )
    full_game, _ = make_game(full)
    left_game, _ = make_game(left)

    offered = [
        selected_tool_names(look(full_game, "b")),
        last_request_tool_names(ResidentTools(full_game, "b")),
        selected_tool_names(look(left_game, "a")),
        last_request_tool_names(ResidentTools(left_game, "a")),
    ]

    assert not any("supply_server" in names for names in offered)


def test_the_last_request_offers_building_supplying_and_pricing_by_the_live_world() -> None:
    game, _ = make_game(HOSTED)

    owner = last_request_tool_names(ResidentTools(game, "a"))
    server_owner = last_request_tool_names(ResidentTools(game, "b"))

    assert "build_meeting_room" in owner and "build_server" not in owner
    assert {"supply_server", "set_hosting_fee"} <= server_owner
