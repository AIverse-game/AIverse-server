from dataclasses import replace

import pytest

from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import (
    WORLD_LOG_LIMIT,
    Inventory,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import AD_WORKLOAD, SELF_RATE
from aiverse_server.game.actions import (
    ActionRejected,
    BuildAd,
    BuyParcel,
    LinkPortals,
    MakeContent,
    MoveTo,
    OfferToBuy,
    ProposeContract,
    RespondToEvent,
    Say,
    Teleport,
    WithdrawEvent,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.world_log import perform_logged


def resident(resident_id: str, position: Position, *items: tuple[str, int]) -> Resident:
    return Resident(resident_id, resident_id, "#123456", position, Inventory(items), coins=10_000)


# a and b stand side by side, c far away; a owns a bare parcel and b has bread to sell.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", Position(55, 35)),
        resident("b", Position(56, 35), ("bread", 5)),
        resident("c", Position(95, 95)),
    ),
    parcels=(Parcel(5, 3, "a"),),
)


def test_a_whisper_is_logged_with_who_said_it_to_whom() -> None:
    after = perform_logged(WORLD, Say("a", "  meet me at noon ", to="c"))

    (entry,) = after.world_log.entries
    assert (entry.verb, entry.actor_id, entry.other_id) == ("said", "a", "c")
    assert entry.text == "meet me at noon"
    assert entry.quantity is None


def test_speaking_aloud_logs_how_many_heard_it() -> None:
    entry = perform_logged(WORLD, Say("a", "hello")).world_log.entries[0]

    assert (entry.verb, entry.other_id, entry.quantity) == ("said", None, 1)


def test_setting_off_logs_where_the_resident_heads() -> None:
    entry = perform_logged(WORLD, MoveTo("c", Position(10, 20))).world_log.entries[0]

    assert (entry.verb, entry.actor_id, entry.position) == ("walked", "c", Position(10, 20))


def test_buying_land_logs_the_parcel_and_what_it_cost() -> None:
    after = perform_logged(WORLD, BuyParcel("c"))

    entry = after.world_log.entries[0]
    assert (entry.verb, entry.parcel_id, entry.quantity) == ("bought_parcel", "L9-9", 1)
    assert entry.coins == coin_balance(WORLD, "c") - coin_balance(after, "c")
    assert entry.coins is not None and entry.coins > 0


def test_an_offer_and_its_acceptance_are_logged_with_the_deal() -> None:
    offered = perform_logged(WORLD, OfferToBuy("a", "b", "bread", 2, 30))
    event_id = offered.events.pending[0].id

    accepted = perform_logged(offered, RespondToEvent("b", event_id, accept=True))

    offer, answer = accepted.world_log.entries
    assert (offer.verb, offer.actor_id, offer.other_id) == ("offered", "a", "b")
    assert (answer.verb, answer.actor_id, answer.other_id) == ("accepted", "b", "a")
    for entry in (offer, answer):
        assert (entry.deal, entry.item, entry.quantity, entry.coins) == ("purchase", "bread", 2, 30)


def test_a_contract_proposal_logs_which_side_the_sender_takes() -> None:
    proposal = ProposeContract("b", "a", True, "bread", 1, 10, deliveries=3, duration=60)

    entry = perform_logged(WORLD, proposal).world_log.entries[0]

    assert (entry.deal, entry.side, entry.deliveries) == ("contract", "sell", 3)


def test_withdrawing_an_offer_logs_who_it_was_for() -> None:
    offered = perform_logged(WORLD, OfferToBuy("a", "b", "bread", 1, 5))
    event_id = offered.events.pending[0].id

    entry = perform_logged(offered, WithdrawEvent("a", event_id)).world_log.entries[-1]

    assert (entry.verb, entry.actor_id, entry.other_id, entry.deal) == (
        "withdrew",
        "a",
        "b",
        "purchase",
    )


def test_teleporting_logs_the_line_s_toll_even_when_the_traveller_gets_it_back() -> None:
    portal = Facility("portal", built_at=0.0)
    world = replace(
        WORLD,
        residents=(resident("a", Position(5, 5)), *WORLD.residents[1:]),
        parcels=(Parcel(0, 0, "a", facility=portal), Parcel(9, 9, "a", facility=portal)),
    )
    linked = perform_logged(world, LinkPortals("a", "L0-0", "L9-9", 25))

    entry = perform_logged(linked, Teleport("a", "L9-9")).world_log.entries[-1]

    assert (entry.verb, entry.parcel_id, entry.other_parcel_id) == ("teleported", "L0-0", "L9-9")
    assert entry.coins == 25


def test_a_refused_primitive_is_not_logged() -> None:
    with pytest.raises(ActionRejected):
        perform_logged(WORLD, Say("a", "   "))

    assert WORLD.world_log.entries == ()


def test_starting_and_finishing_a_build_are_both_logged() -> None:
    started = perform_logged(WORLD, BuildAd("a", "L5-3", "self"))

    done = settle_due(replace(started, time=AD_WORKLOAD / SELF_RATE))

    start, finish = done.world_log.entries
    assert (start.verb, start.parcel_id, start.facility) == ("started_building", "L5-3", "ad")
    assert (finish.verb, finish.actor_id, finish.parcel_id, finish.facility) == (
        "built",
        "a",
        "L5-3",
        "ad",
    )
    assert finish.time == AD_WORKLOAD / SELF_RATE


def test_a_job_dropped_because_another_built_the_parcel_first_is_not_logged_as_built() -> None:
    shared = Parcel(5, 3, "a", shares=(("a", 50), ("b", 50)))
    world = replace(WORLD, parcels=(shared, *WORLD.parcels[1:]))
    started = perform_logged(
        perform_logged(world, BuildAd("a", "L5-3", "self")), BuildAd("b", "L5-3", "self")
    )

    done = settle_due(replace(started, time=AD_WORKLOAD / SELF_RATE))

    built = [entry for entry in done.world_log.entries if entry.verb == "built"]
    assert [(entry.actor_id, entry.parcel_id) for entry in built] == [("a", "L5-3")]


def test_finished_content_is_logged_as_made() -> None:
    started = perform_logged(WORLD, MakeContent("b", "self", content="game"))
    (job,) = started.compute.jobs

    done = settle_due(replace(started, time=job.ends_at))

    entry = done.world_log.entries[-1]
    assert (entry.verb, entry.actor_id, entry.item) == ("made", "b", "game")


def test_the_log_keeps_only_the_latest_entries_numbered_in_order() -> None:
    world = WORLD

    for index in range(WORLD_LOG_LIMIT + 5):
        world = perform_logged(world, Say("a", f"line {index}"))

    entries = world.world_log.entries
    assert len(entries) == WORLD_LOG_LIMIT
    assert entries[0].text == "line 5"
    assert [entry.seq for entry in entries] == list(range(6, WORLD_LOG_LIMIT + 6))
