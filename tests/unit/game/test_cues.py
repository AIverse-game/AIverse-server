from dataclasses import replace

from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.game.actions import OfferToBuy, RespondToEvent, Say, perform
from aiverse_server.game.cues import cues, new_facility_owners

# a wants bread; b has it.
WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(id="a", name="a", color="#ffffff", position=Position(1, 1), coins=20),
        Resident(
            id="b",
            name="b",
            color="#000000",
            position=Position(9, 9),
            inventory=Inventory((("bread", 5),)),
        ),
    ),
)


def test_saying_or_offering_makes_the_speaker_talk() -> None:
    assert cues(WORLD, Say("a", "hi")) == [("a", "talk")]
    assert cues(WORLD, OfferToBuy("a", "b", "bread", 1, 5)) == [("a", "talk")]


def test_accepting_an_offer_makes_both_sides_happy() -> None:
    offered = perform(WORLD, OfferToBuy("a", "b", "bread", 1, 5))
    event_id = offered.events.pending[0].id

    shown = cues(offered, RespondToEvent("b", event_id, accept=True))

    assert shown == [("b", "happy"), ("a", "happy")]


def test_rejecting_an_offer_is_only_talk() -> None:
    offered = perform(WORLD, OfferToBuy("a", "b", "bread", 1, 5))
    event_id = offered.events.pending[0].id

    assert cues(offered, RespondToEvent("b", event_id, accept=False)) == [("b", "talk")]


def test_a_finished_facility_makes_its_owner_happy() -> None:
    parcel = Parcel(col=0, row=0, owner_id="a")
    before = replace(WORLD, parcels=(parcel,))
    after = replace(before, parcels=(replace(parcel, facility=Facility("ad", built_at=0.0)),))

    assert new_facility_owners(before, after) == ["a"]
    assert new_facility_owners(after, after) == []
