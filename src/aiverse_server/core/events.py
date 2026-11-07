"""Events: requests one resident sends another, which wait until the recipient answers.

An event works like a message in a queue. A resident sends one, such as "I would like to buy
your bread for 5 coins"; it waits in the world's queue until the recipient accepts or rejects
it, or the sender withdraws it. Nothing the event asks for happens before it is accepted.

Each kind of event is a subclass of `EventPayload` that holds its details and overrides the
hooks it needs: `on_send` checks the request and reserves what it promises, `on_accept`
carries it out and `on_decline` releases the reservation. This module only runs the queue;
concrete kinds such as purchases live with the rules they belong to.

Like inventories, these are server-side building blocks: every function returns a new world
and leaves its argument untouched, and on error nothing changes, the queue included. Players
and AI answer events through the primitives in `actions.py`; sending will be done by the
primitives of each kind, such as an offer to buy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .world import World

EventErrorCode = Literal["unknown_resident", "unknown_event", "self_addressed", "resident_left"]


class EventError(Exception):
    def __init__(self, code: EventErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: EventErrorCode = code
        self.message = message


class EventPayload:
    """What an event asks for; subclass it once per kind of event.

    Subclasses are frozen dataclasses holding the details, such as the item and price of a
    purchase. Each hook receives the world and the event and returns the next world, or
    raises `EventError`, `InventoryError`, `TradeError`, `ContractError`, `SharesError`,
    `LandError`, `InformationError` or `NoteError` to refuse, which leaves the world and the
    queue as they were; `actions.py` turns these into rejections.
    The defaults change nothing, so a kind overrides only what it needs.
    """

    def on_send(self, world: World, event: Event) -> World:
        """Check that the request can be made and reserve what it promises, such as the
        buyer's coins. Runs before the event joins the queue, which it must leave alone."""
        return world

    def on_accept(self, world: World, event: Event) -> World:
        """Carry the request out. Runs after the event has left the queue; refusing keeps it
        pending, so the recipient can still reject it. May send or answer other events, but
        must not put this one back or wind the queue's ids back."""
        return world

    def on_decline(self, world: World, event: Event) -> World:
        """Release what `on_send` reserved: the recipient rejected the event or the sender
        withdrew it. Runs after the event has left the queue, under the same rules as
        `on_accept`. It should never refuse: declining is the only way out of the queue, so
        an event whose release fails stays pending for good."""
        return world


@dataclass(frozen=True)
class Event:
    id: int
    """Unique within a world and never reused, so an answer cannot reach a later event."""
    sender_id: str
    recipient_id: str
    payload: EventPayload


@dataclass(frozen=True)
class EventQueue:
    """Events waiting for an answer, oldest first, and the id the next one will get."""

    pending: tuple[Event, ...] = ()
    next_id: int = 1

    def __post_init__(self) -> None:
        # Exactly int: bool is an int subclass, and ids are compared and counted up.
        if type(self.next_id) is not int or self.next_id < 1:
            raise ValueError(f"next event id must be an integer >= 1, got {self.next_id!r}")
        previous = 0
        for event in self.pending:
            if type(event.id) is not int or not previous < event.id < self.next_id:
                raise ValueError(
                    f"event id {event.id!r} must be an integer above {previous} "
                    f"and below {self.next_id}"
                )
            previous = event.id


def pending_for(world: World, resident_id: str) -> tuple[Event, ...]:
    """Events waiting for this resident's answer, oldest first."""
    _check_resident(world, resident_id)
    return tuple(event for event in world.events.pending if event.recipient_id == resident_id)


def pending_from(world: World, resident_id: str) -> tuple[Event, ...]:
    """Events this resident sent that are still unanswered, oldest first."""
    _check_resident(world, resident_id)
    return tuple(event for event in world.events.pending if event.sender_id == resident_id)


def send_event(
    world: World, sender_id: str, recipient_id: str, payload: EventPayload
) -> tuple[World, Event]:
    """Queue a request from sender to recipient; returns the next world and the new event."""
    for party in (sender_id, recipient_id):
        _check_resident(world, party)
        # Someone who left the game no longer deals with anyone.
        if any(resident.id == party and resident.left for resident in world.residents):
            raise EventError("resident_left", f"resident {party!r} has left the game")
    if sender_id == recipient_id:
        raise EventError("self_addressed", f"resident {sender_id!r} cannot send an event to itself")
    queue = world.events
    event = Event(id=queue.next_id, sender_id=sender_id, recipient_id=recipient_id, payload=payload)
    after = payload.on_send(world, event)
    if after.events != queue:
        # Ids must stay in order; a kind that needs a follow-up event sends it afterwards.
        raise ValueError(f"{type(payload).__name__}.on_send must not change the event queue")
    pending = (*queue.pending, event)
    return replace(after, events=EventQueue(pending, queue.next_id + 1)), event


def accept_event(world: World, resident_id: str, event_id: int) -> World:
    """The recipient agrees, and the event's request is carried out."""
    event = _find(world, event_id, resident_id, lambda e: e.recipient_id)
    return _close(world, event, event.payload.on_accept)


def reject_event(world: World, resident_id: str, event_id: int) -> World:
    """The recipient refuses; whatever the sender reserved is released."""
    event = _find(world, event_id, resident_id, lambda e: e.recipient_id)
    return _close(world, event, event.payload.on_decline)


def withdraw_event(world: World, resident_id: str, event_id: int) -> World:
    """The sender takes back an event not yet answered; whatever it reserved is released."""
    event = _find(world, event_id, resident_id, lambda e: e.sender_id)
    return _close(world, event, event.payload.on_decline)


def _check_resident(world: World, resident_id: str) -> None:
    if all(resident.id != resident_id for resident in world.residents):
        raise EventError("unknown_resident", f"no resident with id {resident_id!r}")


def _find(world: World, event_id: int, resident_id: str, party: Callable[[Event], str]) -> Event:
    _check_resident(world, resident_id)
    for event in world.events.pending:
        # Someone else's event is reported as missing, so its existence does not leak.
        if event.id == event_id and party(event) == resident_id:
            return event
    raise EventError("unknown_event", f"resident {resident_id!r} has no pending event {event_id!r}")


def _close(world: World, event: Event, hook: Callable[[World, Event], World]) -> World:
    """Take the event off the queue, then run the hook that answers it."""
    queue = world.events
    pending = tuple(other for other in queue.pending if other.id != event.id)
    after = hook(replace(world, events=replace(queue, pending=pending)), event)
    # Otherwise an answer could leave its event pending, or a later event reuse an old id.
    if after.events.next_id < queue.next_id or any(
        other.id == event.id for other in after.events.pending
    ):
        hook_name = f"{type(event.payload).__name__}.{hook.__name__}"
        raise ValueError(f"{hook_name} must not requeue the event or wind ids back")
    return after
