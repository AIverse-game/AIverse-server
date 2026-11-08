from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import MAX_TOLL, Facility, Link, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.ads import pay_visitors
from aiverse_server.economy.compute import PORTAL_WORKLOAD, SELF_RATE
from aiverse_server.economy.footfall import footfall
from aiverse_server.economy.portals import (
    MAX_LINKS_PER_PORTAL,
    MAX_PORTALS,
    MIN_LINK_DISTANCE,
    effective_distance,
)
from aiverse_server.economy.shares import MAX_PENDING_PER_PAIR
from aiverse_server.game.actions import (
    ActionRejected,
    BuildAd,
    BuildPortal,
    BuyNode,
    LeaveGame,
    LinkPortals,
    MoveTo,
    PlaceContent,
    RespondToEvent,
    Teleport,
    WithdrawEvent,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game, total_coins

PORTAL = Facility("portal", 0.0)


def resident(resident_id: str, x: float, y: float, coins: int = 5_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(x, y), coins=coins)


def portal_parcel(col: int, row: int, owner: str, **kwargs: object) -> Parcel:
    return Parcel(col, row, owner, facility=PORTAL, **kwargs)  # type: ignore[arg-type]


# A 10 x 10 grid whose centre is (50, 50); parcel L<col>-<row> is centred at
# (10 col + 5, 10 row + 5). a owns portals on L0-0 (far north-west), L9-9 (far south-east) and
# L5-4 (next to the centre); b owns a portal on L9-0 and a bare parcel L0-9. a stands in L0-0,
# b in L9-0, c in L0-9.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 5, 5),
        resident("b", 95, 5),
        resident("c", 5, 95),
    ),
    parcels=(
        portal_parcel(0, 0, "a"),
        portal_parcel(9, 9, "a"),
        portal_parcel(5, 4, "a"),
        portal_parcel(9, 0, "b"),
        Parcel(0, 9, "b"),
    ),
)


def link(world: World, parcel_id: str, other: str, toll: int = 10, who: str = "a") -> World:
    return perform(world, LinkPortals(who, parcel_id, other, toll))


def coins(world: World, *ids: str) -> tuple[int, ...]:
    return tuple(coin_balance(world, resident_id) for resident_id in ids)


def test_a_portal_is_built_by_a_generative_job_and_appears_when_it_ends() -> None:
    world = replace(WORLD, parcels=(Parcel(2, 2, "a"),))

    started = perform(world, BuildPortal("a", "L2-2", "self"))
    done = settle_due(replace(started, time=PORTAL_WORKLOAD / SELF_RATE))

    assert started.parcels[0].facility is None
    assert done.parcels[0].facility == Facility("portal", PORTAL_WORKLOAD / SELF_RATE)
    assert done.compute.jobs == ()


def test_a_portal_is_built_while_walking_and_takes_the_parcel_like_an_ad_slot() -> None:
    world = replace(WORLD, parcels=(Parcel(2, 2, "a"),))
    walking = perform(world, MoveTo("a", Position(60, 60)))

    started = perform(walking, BuildPortal("a", "L2-2", "self"))

    with pytest.raises(ActionRejected) as raised:
        perform(started, BuildAd("a", "L2-2", "on_demand"))
    assert raised.value.code == "parcel_busy"


def test_a_resident_may_hold_only_so_many_portals_built_or_building() -> None:
    parcels = tuple(portal_parcel(col, 0, "a") for col in range(MAX_PORTALS - 1))
    world = replace(WORLD, parcels=(*parcels, Parcel(0, 5, "a"), Parcel(1, 5, "a")))
    building = perform(world, BuildPortal("a", "L0-5", "self"))

    with pytest.raises(ActionRejected) as raised:
        perform(building, BuildPortal("a", "L1-5", "on_demand"))

    assert raised.value.code == "too_many_portals"


def test_a_portal_shows_no_content_and_is_never_paid() -> None:
    world = replace(WORLD, time=60.0)

    with pytest.raises(ActionRejected) as raised:
        perform(world, PlaceContent("a", "L0-0"))

    assert raised.value.code == "no_content_facility"
    assert pay_visitors(world) == world


def test_two_portals_of_one_resident_are_linked_at_once() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=25)

    assert linked.links == (Link("L0-0", "L9-9", 25),)
    assert linked.events.pending == ()


def test_a_line_does_not_care_which_end_it_is_drawn_from() -> None:
    assert link(WORLD, "L9-9", "L0-0").links == link(WORLD, "L0-0", "L9-9").links


@pytest.mark.parametrize(
    ("parcel", "other", "toll", "code"),
    [
        ("L0-0", "L0-0", 10, "same_portal"),
        ("L0-0", "L0-9", 10, "no_portal"),
        ("L0-0", "L3-3", 10, "unknown_parcel"),
        ("L9-0", "L0-0", 10, "not_your_portal"),
        ("L0-0", "L9-9", -1, "invalid_toll"),
        ("L0-0", "L9-9", MAX_TOLL + 1, "invalid_toll"),
        ("L0-0", "L9-9", True, "invalid_toll"),
    ],
)
def test_lines_that_break_the_rules_are_refused_and_change_nothing(
    parcel: str, other: str, toll: int, code: str
) -> None:
    with pytest.raises(ActionRejected) as raised:
        link(WORLD, parcel, other, toll)

    assert raised.value.code == code


def test_two_portals_cannot_be_linked_twice() -> None:
    linked = link(WORLD, "L0-0", "L9-9")

    with pytest.raises(ActionRejected) as raised:
        link(linked, "L9-9", "L0-0", toll=5)

    assert raised.value.code == "already_linked"


def test_portals_closer_than_the_minimum_distance_cannot_be_linked() -> None:
    near = replace(WORLD, parcels=(portal_parcel(2, 2, "a"), portal_parcel(4, 2, "a")))
    far = replace(WORLD, parcels=(portal_parcel(2, 2, "a"), portal_parcel(5, 2, "a")))

    with pytest.raises(ActionRejected) as raised:
        link(near, "L2-2", "L4-2")

    assert raised.value.code == "too_close"
    assert MIN_LINK_DISTANCE == 30
    assert link(far, "L2-2", "L5-2").links == (Link("L2-2", "L5-2", 10),)


def test_a_portal_ends_only_so_many_lines() -> None:
    hub = portal_parcel(0, 0, "a")
    spokes = tuple(portal_parcel(col, 9, "a") for col in range(MAX_LINKS_PER_PORTAL + 1))
    world = replace(WORLD, parcels=(hub, *spokes))
    for spoke in spokes[:MAX_LINKS_PER_PORTAL]:
        world = link(world, "L0-0", spoke.id)

    with pytest.raises(ActionRejected) as raised:
        link(world, "L0-0", spokes[-1].id)

    assert raised.value.code == "too_many_links"


def test_a_line_to_someone_elses_portal_waits_for_their_answer() -> None:
    proposed = link(WORLD, "L0-0", "L9-0", toll=40)

    assert proposed.links == ()
    assert [(e.sender_id, e.recipient_id) for e in proposed.events.pending] == [("a", "b")]


def test_accepting_a_line_draws_it_and_rejecting_or_withdrawing_does_not() -> None:
    proposed = link(WORLD, "L0-0", "L9-0", toll=40)

    accepted = perform(proposed, RespondToEvent("b", 1, True))
    rejected = perform(proposed, RespondToEvent("b", 1, False))
    withdrawn = perform(proposed, WithdrawEvent("a", 1))

    assert accepted.links == (Link("L0-0", "L9-0", 40),)
    assert accepted.events.pending == ()
    assert (rejected.links, rejected.events.pending) == ((), ())
    assert (withdrawn.links, withdrawn.events.pending) == ((), ())


def test_only_the_owner_of_the_far_portal_can_answer_and_the_proposer_must_own_the_near_one() -> (
    None
):
    proposed = link(WORLD, "L0-0", "L9-0")

    with pytest.raises(ActionRejected) as answered:
        perform(proposed, RespondToEvent("c", 1, True))
    with pytest.raises(ActionRejected) as stolen:
        link(WORLD, "L9-0", "L9-9", who="a")

    assert answered.value.code == "unknown_event"
    assert stolen.value.code == "not_your_portal"


def test_a_proposal_is_checked_again_when_it_is_accepted() -> None:
    spokes = (portal_parcel(9, 5, "b"), portal_parcel(5, 9, "b"), portal_parcel(0, 5, "b"))
    world = replace(WORLD, parcels=(*WORLD.parcels, *spokes))
    proposed = link(world, "L0-0", "L9-0")
    # While it waits, b joins L9-0 to three portals of their own.
    crowded = replace(proposed, links=tuple(Link("L9-0", spoke.id, 5) for spoke in spokes))

    with pytest.raises(ActionRejected) as raised:
        perform(crowded, RespondToEvent("b", 1, True))

    assert raised.value.code == "too_many_links"
    assert len(crowded.events.pending) == 1


def test_proposals_to_one_resident_are_limited_in_number() -> None:
    parcels = tuple(portal_parcel(col, 9, "b") for col in range(MAX_PENDING_PER_PAIR + 1))
    world = replace(WORLD, parcels=(portal_parcel(0, 0, "a"), *parcels))
    for parcel in parcels[:MAX_PENDING_PER_PAIR]:
        world = link(world, "L0-0", parcel.id)

    with pytest.raises(ActionRejected) as raised:
        link(world, "L0-0", parcels[-1].id)

    assert raised.value.code == "too_many_offers"


def test_a_proposal_goes_when_its_sender_leaves() -> None:
    proposed = link(WORLD, "L0-0", "L9-0")

    left = perform(proposed, LeaveGame("a"))

    assert (left.events.pending, left.links) == ((), ())


def test_ending_the_game_clears_waiting_proposals() -> None:
    assert end_game(link(WORLD, "L0-0", "L9-0")).events.pending == ()


def test_a_resident_standing_in_a_portal_parcel_travels_to_the_far_end_at_once() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=0)

    arrived = perform(linked, Teleport("a", "L9-9"))
    back = perform(arrived, Teleport("a", "L0-0"))

    assert [r.position for r in arrived.residents if r.id == "a"] == [Position(95, 95)]
    assert [r.position for r in back.residents if r.id == "a"] == [Position(5, 5)]
    assert back.time == linked.time


def test_the_toll_is_paid_to_the_holders_of_both_ends_half_each() -> None:
    # b holds the whole of L9-0 and half of L0-0's shares; a holds the rest of L0-0.
    shared = replace(
        WORLD,
        parcels=(
            portal_parcel(0, 0, "a", shares=(("a", 50), ("b", 50))),
            portal_parcel(9, 0, "b"),
        ),
    )
    shared = replace(shared, residents=(*shared.residents[:2], resident("c", 5, 5)))
    linked = perform(link(shared, "L0-0", "L9-0", toll=101, who="a"), RespondToEvent("b", 1, True))

    traveller = perform(linked, Teleport("c", "L9-0"))

    # 101 coins: 51 to the departure end, split 25.5 / 25.5 with the odd coin to a (the holder id
    # settles a tie), and 50 to the arrival end, all b's.
    paid = sorted((t.payee, t.amount, t.channel) for t in traveller.ledger.transfers)
    assert coins(traveller, "c") == (5_000 - 101,)
    assert sum(amount for _, amount, _ in paid) == 101
    assert {channel for _, _, channel in paid} == {"toll"}
    assert coins(traveller, "a", "b") == (5_000 + 26, 5_000 + 75)
    assert all(t.payer == "c" for t in traveller.ledger.transfers)


def test_the_odd_coin_of_a_toll_goes_to_the_departure_end() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=3)
    traveller = replace(
        linked,
        residents=tuple(
            replace(r, position=Position(5, 5)) if r.id == "b" else r for r in linked.residents
        ),
    )
    linked_b = replace(
        traveller,
        parcels=tuple(
            replace(p, owner_id="b", shares=()) if p.id == "L9-9" else p for p in traveller.parcels
        ),
    )

    paid = perform(linked_b, Teleport("b", "L9-9"))

    assert coins(paid, "a", "b") == (5_000 + 2, 5_000 - 3 + 1)


def test_a_toll_a_resident_owes_themselves_is_not_paid_or_recorded() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=100)

    arrived = perform(linked, Teleport("a", "L9-9"))

    assert coins(arrived, "a") == (5_000,)
    assert arrived.ledger.transfers == ()


def test_the_toll_is_taken_in_full_or_the_trip_is_refused() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=100)
    poor = replace(
        linked,
        residents=tuple(
            replace(r, coins=50, position=Position(5, 5)) if r.id == "b" else r
            for r in linked.residents
        ),
    )

    with pytest.raises(ActionRejected):
        perform(poor, Teleport("b", "L9-9"))


def test_a_trip_needs_a_portal_here_a_line_and_a_resident_who_is_not_walking() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=0)
    walking = perform(linked, MoveTo("a", Position(6, 6)))
    elsewhere = perform(linked, MoveTo("c", Position(5, 95)))

    codes = []
    for world, who, target in (
        (linked, "c", "L9-9"),  # c stands in L0-9, which has no portal
        (linked, "a", "L5-4"),  # a portal, but no line to it
        (linked, "a", "L3-3"),  # not even a parcel
        (walking, "a", "L9-9"),  # busy
        (elsewhere, "c", "L0-0"),
    ):
        with pytest.raises(ActionRejected) as raised:
            perform(world, Teleport(who, target))
        codes.append(raised.value.code)

    assert codes == ["not_at_portal", "not_linked", "not_linked", "busy", "not_at_portal"]


def test_a_trip_is_logged_as_a_move_for_those_nearby() -> None:
    linked = link(WORLD, "L0-0", "L9-9", toll=0)

    arrived = perform(linked, Teleport("a", "L9-9"))

    entry = arrived.activity.entries[-1]
    assert (entry.kind, entry.position, entry.origin) == (
        "moved",
        Position(95, 95),
        Position(5, 5),
    )


def test_coins_are_conserved_by_a_trip_and_the_ranking_is_unchanged() -> None:
    linked = link(WORLD, "L0-0", "L9-0", toll=0)
    accepted = perform(linked, RespondToEvent("b", 1, True))
    paying = replace(
        accepted,
        links=(Link("L0-0", "L9-0", 77),),
        residents=tuple(
            replace(r, position=Position(5, 5)) if r.id == "c" else r for r in accepted.residents
        ),
    )

    after = perform(paying, Teleport("c", "L9-0"))

    assert sum(total_coins(after, r.id) for r in after.residents) == 15_000
    assert SYSTEM not in {t.payee for t in after.ledger.transfers}


def test_without_lines_the_effective_distance_is_the_straight_one() -> None:
    assert effective_distance(WORLD, Position(5, 5)) == pytest.approx(63.639610306789276)


def test_a_line_to_a_portal_by_the_centre_brings_a_remote_portal_close_to_it() -> None:
    linked = link(WORLD, "L0-0", "L5-4")

    # L5-4's centre is (55, 45), about 7.07 from the middle.
    assert effective_distance(linked, Position(5, 5)) == pytest.approx(7.0710678)
    assert effective_distance(linked, Position(5, 15)) == pytest.approx(10 + 7.0710678)


def test_the_effective_distance_goes_along_lines_that_follow_one_another() -> None:
    chained = link(link(WORLD, "L9-9", "L0-0"), "L0-0", "L5-4")

    assert effective_distance(chained, Position(95, 95)) == pytest.approx(7.0710678)


def test_a_line_never_makes_a_spot_farther_and_only_helps_next_to_a_portal() -> None:
    linked = link(WORLD, "L0-0", "L5-4")
    spots = (Position(50, 50), Position(55, 45), Position(95, 5), Position(5, 95))

    for spot in spots:
        assert effective_distance(linked, spot) <= effective_distance(WORLD, spot)
    assert effective_distance(linked, Position(95, 5)) == effective_distance(WORLD, Position(95, 5))


def test_footfall_ignores_lines_since_visitors_are_not_simulated_yet() -> None:
    linked = link(WORLD, "L0-0", "L5-4")
    spot = Position(5, 22)  # 17 from the resident in L0-0, so no resident adds to it

    assert footfall(linked, spot) == footfall(WORLD, spot)
    assert effective_distance(linked, spot) < effective_distance(WORLD, spot)


def test_the_effective_distance_does_not_change_with_who_is_standing_where() -> None:
    linked = link(WORLD, "L0-0", "L5-4")
    moved = perform(linked, MoveTo("c", Position(1, 1)))

    assert effective_distance(moved, Position(5, 5)) == effective_distance(linked, Position(5, 5))


def test_a_world_with_a_line_to_a_parcel_without_a_portal_is_not_valid() -> None:
    with pytest.raises(ValueError, match="without a portal"):
        replace(WORLD, links=(Link("L0-0", "L0-9", 5),))


def test_a_world_with_the_same_line_twice_is_not_valid() -> None:
    with pytest.raises(ValueError, match="linked twice"):
        replace(WORLD, links=(Link("L0-0", "L9-9", 5), Link("L9-9", "L0-0", 7)))


def test_a_line_is_stored_with_its_ends_in_order_and_needs_two_different_parcels() -> None:
    assert Link("L9-9", "L0-0", 3) == Link("L0-0", "L9-9", 3)
    with pytest.raises(ValueError, match="two different parcels"):
        Link("L0-0", "L0-0", 3)


def test_a_portal_cannot_hold_content_or_have_a_payday() -> None:
    with pytest.raises(ValueError, match="shows no content"):
        Facility("portal", 0.0, (100.0,))
    with pytest.raises(ValueError, match="never paid"):
        Facility("portal", 0.0, (), 30.0)


def test_a_line_cannot_be_proposed_to_a_resident_who_left() -> None:
    gone = perform(WORLD, LeaveGame("b"))

    with pytest.raises(ActionRejected) as raised:
        link(gone, "L0-0", "L9-0")

    assert raised.value.code == "resident_left"
    assert gone.events.pending == ()


def test_a_portal_needs_a_generative_node() -> None:
    world = replace(WORLD, parcels=(Parcel(2, 2, "a"),))
    bought = perform(world, BuyNode("a", "render", 60))

    with pytest.raises(ActionRejected) as raised:
        perform(bought, BuildPortal("a", "L2-2", "subscription", 1))

    assert raised.value.code == "wrong_node_kind"


def test_a_traveller_who_holds_part_of_a_departure_end_gets_that_part_of_its_half_back() -> None:
    shared = replace(
        WORLD,
        parcels=(
            portal_parcel(0, 0, "a", shares=(("a", 50), ("c", 50))),
            portal_parcel(9, 9, "a"),
        ),
        residents=tuple(
            replace(r, position=Position(5, 5)) if r.id == "c" else r for r in WORLD.residents
        ),
    )
    linked = link(shared, "L0-0", "L9-9", toll=100)

    after = perform(linked, Teleport("c", "L9-9"))

    # c pays 100; the departure end's 50 splits 25 / 25 and the arrival end's 50 is all a's.
    assert coins(after, "a", "c") == (5_000 + 25 + 50, 5_000 - 100 + 25)
    assert [(t.payer, t.payee, t.amount) for t in after.ledger.transfers] == [
        ("c", "a", 25),
        ("c", "a", 50),
    ]
    assert sum(total_coins(after, r.id) for r in after.residents) == 15_000
