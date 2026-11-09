"""Portals, lines and trips through the agent tools, and what a model is told about them."""

from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.tool_selection import (
    TOOL_GROUPS,
    discover_tools,
    last_request_tool_names,
    selected_tool_names,
)
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.land import Facility, Link, Parcel
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.compute import ON_DEMAND_PRICE, PORTAL_WORKLOAD, SELF_RATE
from aiverse_server.game.actions import LeaveGame, perform
from aiverse_server.server.app import Game

PORTAL = Facility("portal", 0.0)

# A 10 x 10 grid. a owns portals on L0-0 and L9-9, a bare parcel L5-5 and an ad slot on L2-7;
# b owns a portal on L9-0. a stands in L0-0, b in L9-0, c in L0-9.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(5, 5), coins=5_000),
        Resident("b", "乙", "#445566", Position(95, 5), coins=5_000),
        Resident("c", "丙", "#778899", Position(5, 95), coins=5_000),
    ),
    parcels=(
        Parcel(0, 0, "a", facility=PORTAL),
        Parcel(9, 9, "a", facility=PORTAL),
        Parcel(5, 5, "a"),
        Parcel(2, 7, "a", facility=Facility("ad", 0.0)),
        Parcel(9, 0, "b", facility=PORTAL),
    ),
)


class FakeClock:
    def __call__(self) -> float:
        return 0.0


def make_game(world: World = WORLD) -> Game:
    return Game(world, 600, FakeClock())


def look(game: Game, resident_id: str) -> Observation:
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_a_portal_is_built_through_the_tools_and_appears_when_the_job_ends() -> None:
    game = make_game()
    tools = ResidentTools(game, "a")

    started = tools.build_portal("L5-5", "self")
    game.world = replace(game.world, time=PORTAL_WORKLOAD / SELF_RATE)
    game.tick()

    assert started.ok
    assert "portal on L5-5" in started.message
    facility = next(p for p in game.world.parcels if p.id == "L5-5").facility
    assert facility is not None and facility.kind == "portal"


def test_a_portal_cannot_be_built_on_a_parcel_with_a_facility() -> None:
    result = ResidentTools(make_game(), "a").build_portal("L9-9", "self")

    assert (result.ok, result.code) == (False, "parcel_built")


def test_two_portals_of_one_resident_are_linked_at_once_through_the_tools() -> None:
    game = make_game()

    result = ResidentTools(game, "a").link_portals("L0-0", "L9-9", 20)

    assert result.ok
    assert "now linked" in result.message
    assert game.world.links == (Link("L0-0", "L9-9", 20),)


def test_a_line_to_another_residents_portal_is_proposed_and_they_can_accept_it() -> None:
    game = make_game()

    proposed = ResidentTools(game, "a").link_portals("L0-0", "L9-0", 30)
    to_b = look(game, "b")
    from_a = look(game, "a")
    accepted = ResidentTools(game, "b").call("respond_to_offer", {"event_id": 1, "accept": True})

    assert proposed.ok and "waits for their answer" in proposed.message
    (proposal,) = to_b.link_proposals_to_me
    assert (proposal.from_parcel, proposal.to_parcel, proposal.toll) == ("L0-0", "L9-0", 30)
    assert from_a.my_link_proposals == to_b.link_proposals_to_me
    assert accepted.ok
    assert game.world.links == (Link("L0-0", "L9-0", 30),)


def test_a_line_proposal_can_be_withdrawn() -> None:
    game = make_game()
    ResidentTools(game, "a").link_portals("L0-0", "L9-0", 30)

    withdrawn = ResidentTools(game, "a").call("withdraw_offer", {"event_id": 1})

    assert withdrawn.ok
    assert (game.world.links, game.world.events.pending) == ((), ())


def test_bad_lines_come_back_as_failed_results_and_the_world_stays() -> None:
    game = make_game()
    tools = ResidentTools(game, "a")

    results = [
        tools.link_portals("L0-0", "L0-0", 5),
        tools.link_portals("L0-0", "L5-5", 5),
        tools.link_portals("L0-0", "L9-9", 5_000),
        tools.call("link_portals", {"parcel_id": "L0-0", "other_parcel_id": "L9-9", "toll": "5"}),
    ]

    assert [result.code for result in results] == [
        "same_portal",
        "no_portal",
        "invalid_toll",
        "invalid_arguments",
    ]
    assert (game.world.links, game.world.events.pending) == ((), ())


def test_a_trip_moves_the_traveller_and_charges_the_toll_through_the_tools() -> None:
    world = replace(WORLD, links=(Link("L0-0", "L9-0", 40),))
    game = make_game(world)

    arrived = ResidentTools(game, "a").teleport("L9-0")

    assert arrived.ok
    me = next(r for r in game.world.residents if r.id == "a")
    assert me.position == Position(95, 5)
    # a pays 40 and, holding the departure end alone, gets back its half of it (20).
    assert me.coins == 5_000 - 40 + 20


def test_a_trip_without_a_line_or_a_portal_fails() -> None:
    game = make_game(replace(WORLD, links=(Link("L0-0", "L9-0", 40),)))

    no_line = ResidentTools(game, "a").teleport("L9-9")
    no_portal = ResidentTools(game, "c").teleport("L0-0")

    assert (no_line.ok, no_line.code) == (False, "not_linked")
    assert (no_portal.ok, no_portal.code) == (False, "not_at_portal")


def test_the_observation_lists_portals_lines_and_where_the_resident_stands() -> None:
    game = make_game(replace(WORLD, links=(Link("L0-0", "L9-0", 40),)))

    seen = look(game, "a").transport

    assert seen is not None
    assert seen.here is not None and seen.here.parcel == "L0-0"
    assert [(line.to_parcel, line.to_owner, line.toll) for line in seen.here.lines] == [
        ("L9-0", "b", 40)
    ]
    assert [portal.parcel for portal in seen.mine] == ["L0-0", "L9-9"]
    assert [portal.parcel for portal in seen.others] == ["L9-0"]


def test_a_line_does_not_change_the_people_the_observation_counts_there_now() -> None:
    near = Parcel(5, 4, "b", facility=PORTAL)
    unlinked = replace(WORLD, parcels=(*WORLD.parcels, near))
    linked = replace(unlinked, links=(Link("L0-0", "L5-4", 5),))

    before = look(make_game(unlinked), "a").transport
    after = look(make_game(linked), "a").transport

    assert before is not None and after is not None
    assert after.mine[0].people_near == before.mine[0].people_near


def test_the_observation_has_no_portal_here_while_walking_or_in_an_ordinary_parcel() -> None:
    game = make_game()
    ResidentTools(game, "c")

    in_a_parcel_without = look(game, "c").transport

    assert in_a_parcel_without is not None and in_a_parcel_without.here is None


def test_the_observation_shows_what_a_portal_costs_to_build() -> None:
    observation = look(make_game(), "a")

    options = observation.compute.portal

    assert [(option.source, option.seconds, option.coins) for option in options] == [
        ("self", 360.0, 0),
        ("on_demand", 18.0, PORTAL_WORKLOAD * ON_DEMAND_PRICE),
        ("subscription", 18.0, 0),
    ]
    assert '"portal":' in to_json(observation)


def test_a_portal_is_not_an_ad_slot_for_the_tools_that_pick_one() -> None:
    game = make_game(replace(WORLD, parcels=(Parcel(0, 0, "a", facility=PORTAL),)))

    names = selected_tool_names(look(game, "a"))

    assert "place_content" not in names
    assert "build_ad" not in names  # no bare parcel
    assert "link_portals" not in names  # one portal, none of anyone else's


def test_link_portals_is_offered_with_two_portals_or_someone_elses_to_join() -> None:
    two_of_mine = selected_tool_names(look(make_game(), "a"))
    one_and_theirs = selected_tool_names(
        look(
            make_game(
                replace(WORLD, parcels=(Parcel(0, 0, "a", facility=PORTAL), WORLD.parcels[-1]))
            ),
            "a",
        )
    )
    nothing_of_mine = selected_tool_names(look(make_game(), "c"))

    assert "link_portals" in two_of_mine
    assert "link_portals" in one_and_theirs
    assert "link_portals" not in nothing_of_mine


def test_teleport_is_offered_only_where_a_line_starts() -> None:
    game = make_game(replace(WORLD, links=(Link("L0-0", "L9-0", 40),)))

    at_a_line = selected_tool_names(look(game, "a"))
    elsewhere = selected_tool_names(look(game, "c"))

    assert "teleport" in at_a_line
    assert "teleport" not in elsewhere


def test_the_final_selection_agrees_about_links_and_trips() -> None:
    game = make_game(replace(WORLD, links=(Link("L0-0", "L9-0", 40),)))

    names = last_request_tool_names(ResidentTools(game, "a"))
    others = last_request_tool_names(ResidentTools(game, "c"))

    assert {"teleport", "link_portals"} <= names
    assert "teleport" not in others
    assert "place_content" not in last_request_tool_names(
        ResidentTools(make_game(replace(WORLD, parcels=(Parcel(0, 0, "a", facility=PORTAL),))), "a")
    )


def test_a_line_proposal_can_be_answered_and_withdrawn_so_those_tools_are_offered() -> None:
    game = make_game()
    ResidentTools(game, "a").link_portals("L0-0", "L9-0", 30)

    to_b = selected_tool_names(look(game, "b"))
    from_a = selected_tool_names(look(game, "a"))

    assert "respond_to_offer" in to_b
    assert "withdraw_offer" in from_a


def test_the_transport_group_can_be_discovered() -> None:
    names: set[str] = set()

    reply = discover_tools({"group": "transport"}, names)

    assert names == {"build_portal", "link_portals", "teleport"}
    assert TOOL_GROUPS["transport"] == ("build_portal", "link_portals", "teleport")
    assert '"ok":true' in reply


def test_a_portal_whose_owner_left_is_not_offered_as_a_line_to_draw() -> None:
    gone = perform(WORLD, LeaveGame("b"))
    only_one_of_mine = replace(gone, parcels=tuple(p for p in gone.parcels if p.id != "L9-9"))
    game = make_game(only_one_of_mine)

    seen = look(game, "a").transport
    names = selected_tool_names(look(game, "a"))
    final = last_request_tool_names(ResidentTools(game, "a"))

    assert seen is not None and seen.others == ()
    assert "link_portals" not in names | final
