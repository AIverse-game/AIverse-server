from dataclasses import dataclass, replace

import pytest

from aiverse_server.events import (
    Event,
    EventError,
    EventPayload,
    EventQueue,
    accept_event,
    pending_for,
    pending_from,
    reject_event,
    send_event,
    withdraw_event,
)
from aiverse_server.inventory import InventoryError, list_items, store_item, take_item
from aiverse_server.layout import create_default_world
from aiverse_server.world import Inventory, Position, Resident, World, WorldMap

WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(
            id="a",
            name="a",
            color="#ffffff",
            position=Position(1, 1),
            inventory=Inventory((("bread", 3),)),
        ),
        Resident(id="b", name="b", color="#000000", position=Position(9, 9)),
        Resident(id="c", name="c", color="#123456", position=Position(5, 5)),
    ),
)


@dataclass(frozen=True)
class Note(EventPayload):
    """A kind that keeps every default hook: answering it changes nothing else."""

    text: str = ""


@dataclass(frozen=True)
class Gift(EventPayload):
    """The sender sets the items aside on sending; they go to the recipient on accept and
    back to the sender on decline, as a purchase would do with coins."""

    kind: str
    quantity: int

    def on_send(self, world: World, event: Event) -> World:
        return take_item(world, event.sender_id, self.kind, self.quantity)

    def on_accept(self, world: World, event: Event) -> World:
        return store_item(world, event.recipient_id, self.kind, self.quantity)

    def on_decline(self, world: World, event: Event) -> World:
        return store_item(world, event.sender_id, self.kind, self.quantity)


@dataclass(frozen=True)
class NeedsBread(EventPayload):
    """Accepting costs the recipient one bread, so it fails when they have none."""

    def on_accept(self, world: World, event: Event) -> World:
        return take_item(world, event.recipient_id, "bread")


def test_worlds_start_with_an_empty_queue() -> None:
    world = create_default_world()
    assert world.events == EventQueue()
    assert all(pending_for(world, r.id) == () for r in world.residents)


def test_send_event_queues_it_with_increasing_ids() -> None:
    world, first = send_event(WORLD, "a", "b", Note("hi"))
    world, second = send_event(world, "c", "b", Note("hello"))
    world, third = send_event(world, "b", "a", Note())
    assert [first.id, second.id, third.id] == [1, 2, 3]
    assert first == Event(id=1, sender_id="a", recipient_id="b", payload=Note("hi"))
    assert pending_for(world, "b") == (first, second)
    assert pending_from(world, "b") == (third,)
    assert pending_for(world, "c") == ()
    assert WORLD.events == EventQueue()


def test_ids_are_not_reused_after_an_event_is_answered() -> None:
    world, first = send_event(WORLD, "a", "b", Note())
    world = reject_event(world, "b", first.id)
    world, second = send_event(world, "a", "b", Note())
    assert second.id == 2
    with pytest.raises(EventError) as raised:
        accept_event(world, "b", first.id)
    assert raised.value.code == "unknown_event"


def test_on_send_reserves_and_on_accept_delivers() -> None:
    world, gift = send_event(WORLD, "a", "b", Gift("bread", 2))
    assert list_items(world, "a") == {"bread": 1}
    assert list_items(world, "b") == {}
    world = accept_event(world, "b", gift.id)
    assert list_items(world, "a") == {"bread": 1}
    assert list_items(world, "b") == {"bread": 2}
    assert world.events.pending == ()


@pytest.mark.parametrize("decline", ["reject", "withdraw"])
def test_declining_releases_the_reservation(decline: str) -> None:
    world, gift = send_event(WORLD, "a", "b", Gift("bread", 2))
    if decline == "reject":
        world = reject_event(world, "b", gift.id)
    else:
        world = withdraw_event(world, "a", gift.id)
    assert list_items(world, "a") == {"bread": 3}
    assert list_items(world, "b") == {}
    assert world.events.pending == ()


def test_a_refused_send_queues_nothing() -> None:
    with pytest.raises(InventoryError) as raised:
        send_event(WORLD, "b", "a", Gift("bread", 1))
    assert raised.value.code == "not_enough_items"


def test_a_failed_accept_keeps_the_event_pending() -> None:
    world, event = send_event(WORLD, "a", "b", NeedsBread())
    with pytest.raises(InventoryError):
        accept_event(world, "b", event.id)
    # Still answerable: once b has bread the accept goes through; rejecting also works.
    assert pending_for(world, "b") == (event,)
    assert reject_event(world, "b", event.id).events.pending == ()
    after = accept_event(store_item(world, "b", "bread"), "b", event.id)
    assert list_items(after, "b") == {}
    assert after.events.pending == ()


def test_answering_removes_only_that_event() -> None:
    world, first = send_event(WORLD, "a", "b", Note("1"))
    world, second = send_event(world, "c", "b", Note("2"))
    world, third = send_event(world, "a", "c", Note("3"))
    world = accept_event(world, "b", second.id)
    assert world.events.pending == (first, third)


def test_only_the_recipient_answers_and_only_the_sender_withdraws() -> None:
    world, event = send_event(WORLD, "a", "b", Note())
    for answer, resident in [
        (accept_event, "a"),
        (accept_event, "c"),
        (reject_event, "a"),
        (withdraw_event, "b"),
        (withdraw_event, "c"),
    ]:
        with pytest.raises(EventError) as raised:
            answer(world, resident, event.id)
        assert raised.value.code == "unknown_event"


def test_unknown_residents_are_refused() -> None:
    world, event = send_event(WORLD, "a", "b", Note())
    with pytest.raises(EventError) as raised:
        send_event(WORLD, "a", "nobody", Note())
    assert raised.value.code == "unknown_resident"
    with pytest.raises(EventError) as raised:
        send_event(WORLD, "nobody", "a", Note())
    assert raised.value.code == "unknown_resident"
    with pytest.raises(EventError) as raised:
        accept_event(world, "nobody", event.id)
    assert raised.value.code == "unknown_resident"
    with pytest.raises(EventError) as raised:
        pending_for(world, "nobody")
    assert raised.value.code == "unknown_resident"


def test_residents_cannot_send_events_to_themselves() -> None:
    with pytest.raises(EventError) as raised:
        send_event(WORLD, "a", "a", Note())
    assert raised.value.code == "self_addressed"


def test_on_send_must_not_change_the_queue() -> None:
    @dataclass(frozen=True)
    class Chain(EventPayload):
        def on_send(self, world: World, event: Event) -> World:
            return send_event(world, event.sender_id, "c", Note())[0]

    with pytest.raises(ValueError, match="must not change the event queue"):
        send_event(WORLD, "a", "b", Chain())


@pytest.mark.parametrize("answer", ["accept", "reject"])
def test_answer_hooks_must_not_requeue_or_wind_ids_back(answer: str) -> None:
    @dataclass(frozen=True)
    class Reset(EventPayload):
        def on_accept(self, world: World, event: Event) -> World:
            return replace(world, events=EventQueue())

    @dataclass(frozen=True)
    class Requeue(EventPayload):
        def on_decline(self, world: World, event: Event) -> World:
            return replace(world, events=EventQueue((event,), world.events.next_id))

    if answer == "accept":
        world, event = send_event(WORLD, "a", "b", Reset())
        with pytest.raises(ValueError, match="must not requeue"):
            accept_event(world, "b", event.id)
    else:
        world, event = send_event(WORLD, "a", "b", Requeue())
        with pytest.raises(ValueError, match="must not requeue"):
            reject_event(world, "b", event.id)


def test_answer_hooks_may_send_follow_up_events() -> None:
    @dataclass(frozen=True)
    class Thanks(EventPayload):
        def on_accept(self, world: World, event: Event) -> World:
            return send_event(world, event.recipient_id, event.sender_id, Note("thanks"))[0]

    world, event = send_event(WORLD, "a", "b", Thanks())
    world = accept_event(world, "b", event.id)
    assert world.events.pending == (
        Event(id=2, sender_id="b", recipient_id="a", payload=Note("thanks")),
    )


def test_event_queue_rejects_ids_out_of_order_or_not_yet_issued() -> None:
    note = Event(id=1, sender_id="a", recipient_id="b", payload=Note())
    later = replace(note, id=2)
    assert EventQueue((note, later), next_id=3).pending == (note, later)
    for pending, next_id in [((later, note), 3), ((note, note), 3), ((later,), 2), ((), 0)]:
        with pytest.raises(ValueError):
            EventQueue(pending, next_id)
    with pytest.raises(ValueError):
        EventQueue((), True)


def test_world_rejects_events_between_unknown_or_identical_residents() -> None:
    for sender, recipient in [("a", "nobody"), ("nobody", "a"), ("a", "a")]:
        event = Event(id=1, sender_id=sender, recipient_id=recipient, payload=Note())
        with pytest.raises(ValueError):
            replace(WORLD, events=EventQueue((event,), next_id=2))
