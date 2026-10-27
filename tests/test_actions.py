import math
from dataclasses import dataclass

import pytest

from aiverse_server.actions import ActionRejected, MoveTo, RespondToEvent, WithdrawEvent, perform
from aiverse_server.events import Event, EventPayload, send_event
from aiverse_server.inventory import item_count, store_item, take_item
from aiverse_server.world import Position, Resident, World, WorldMap

WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(id="a", name="a", color="#ffffff", position=Position(1, 1)),
        Resident(id="b", name="b", color="#000000", position=Position(9, 9)),
    ),
)


def test_move_to_puts_only_that_resident_at_the_target() -> None:
    after = perform(WORLD, MoveTo(resident_id="a", target=Position(3.5, 7)))
    assert [r.position for r in after.residents] == [Position(3.5, 7), Position(9, 9)]
    assert after.map == WORLD.map
    assert WORLD.residents[0].position == Position(1, 1)


@pytest.mark.parametrize(("x", "y"), [(0, 0), (10, 10), (0, 10)])
def test_move_to_accepts_the_map_edges(x: float, y: float) -> None:
    after = perform(WORLD, MoveTo(resident_id="b", target=Position(x, y)))
    assert after.residents[1].position == Position(x, y)


@pytest.mark.parametrize(("x", "y"), [(-0.1, 5), (5, 10.1), (math.nan, 5), (5, math.inf)])
def test_move_to_rejects_targets_outside_the_map(x: float, y: float) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, MoveTo(resident_id="a", target=Position(x, y)))
    assert raised.value.code == "out_of_bounds"


def test_move_to_rejects_unknown_residents() -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, MoveTo(resident_id="nobody", target=Position(5, 5)))
    assert raised.value.code == "unknown_resident"


@dataclass(frozen=True)
class Toll(EventPayload):
    """Accepting costs the recipient one coin, so it fails when they have none."""

    def on_accept(self, world: World, event: Event) -> World:
        return take_item(world, event.recipient_id, "coin")


@pytest.mark.parametrize(
    "answer",
    [
        RespondToEvent(resident_id="b", event_id=1, accept=False),
        WithdrawEvent(resident_id="a", event_id=1),
    ],
)
def test_answering_an_event_takes_it_off_the_queue(answer: RespondToEvent | WithdrawEvent) -> None:
    world, _ = send_event(WORLD, "a", "b", Toll())
    assert perform(world, answer).events.pending == ()


def test_accepting_an_event_carries_it_out() -> None:
    world, event = send_event(store_item(WORLD, "b", "coin"), "a", "b", Toll())
    after = perform(world, RespondToEvent(resident_id="b", event_id=event.id, accept=True))
    assert item_count(after, "b", "coin") == 0
    assert after.events.pending == ()


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (RespondToEvent(resident_id="b", event_id=1, accept=True), "not_enough_items"),
        (RespondToEvent(resident_id="a", event_id=1, accept=True), "unknown_event"),
        (RespondToEvent(resident_id="b", event_id=2, accept=False), "unknown_event"),
        (WithdrawEvent(resident_id="b", event_id=1), "unknown_event"),
        (WithdrawEvent(resident_id="nobody", event_id=1), "unknown_resident"),
    ],
)
def test_refused_answers_become_rejections(
    answer: RespondToEvent | WithdrawEvent, code: str
) -> None:
    world, _ = send_event(WORLD, "a", "b", Toll())
    with pytest.raises(ActionRejected) as raised:
        perform(world, answer)
    assert raised.value.code == code
