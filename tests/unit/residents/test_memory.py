from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.core.world import (
    NOTE_LENGTH_LIMIT,
    NOTE_LIMITS,
    Inventory,
    Memory,
    Note,
    NoteTerm,
    Position,
    Resident,
    Spring,
    World,
    WorldMap,
)
from aiverse_server.economy.energy import ENERGY
from aiverse_server.game.actions import Harvest, LeaveGame, MoveTo, perform
from aiverse_server.game.lifecycle import GameError, end_game
from aiverse_server.residents.memory import SHORT_TERM_SECONDS, NoteError, forget, remember
from aiverse_server.residents.perception import begin_turn
from aiverse_server.server.app import Game, Outbox


def resident(resident_id: str, x: float, y: float) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id.upper(),
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((ENERGY, 1000), ("C1", 1))),
    )


WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 10, 10), resident("b", 80, 80)),
    springs=(Spring("near", Position(12, 10), radius=4, reserve=100),),
    time=5.0,
)


def at(world: World, time: float) -> World:
    return replace(world, time=time)


def test_the_resident_chooses_how_long_each_note_lasts() -> None:
    world, plan = remember(WORLD, "a", "  sell energy to b above 3 coins  ", "long_term")
    world, sight = remember(world, "a", "b is heading north", "short_term")

    _, seen = begin_turn(world, "a")

    assert plan == Note(1, "sell energy to b above 3 coins", 5.0, None)
    assert sight == Note(2, "b is heading north", 5.0, 5.0 + SHORT_TERM_SECONDS)
    assert seen.long_term == (plan,)
    assert seen.short_term == (sight,)
    # Nobody else's memory changes.
    assert world.residents[1].memory == Memory()


def test_short_term_notes_expire_and_long_term_notes_last_the_game() -> None:
    world, _ = remember(WORLD, "a", "keep", "long_term")
    world, _ = remember(world, "a", "fleeting", "short_term")
    expires = 5.0 + SHORT_TERM_SECONDS

    world, before = begin_turn(at(world, expires - 0.01), "a")
    world, after = begin_turn(at(world, expires), "a")
    cleared = world.residents[0].memory.short_term
    world, much_later = begin_turn(at(world, 1000.0), "a")

    assert [note.text for note in before.short_term] == ["fleeting"]
    assert after.short_term == ()
    assert cleared == ()
    assert [note.text for note in much_later.long_term] == ["keep"]


def test_notes_come_back_oldest_first_with_ids_shared_across_terms() -> None:
    world = WORLD
    for text, term in (("1", "short_term"), ("2", "long_term"), ("3", "short_term")):
        world, _ = remember(world, "a", text, term)  # type: ignore[arg-type]
    _, seen = begin_turn(world, "a")
    assert [(note.id, note.text) for note in seen.short_term] == [(1, "1"), (3, "3")]
    assert [(note.id, note.text) for note in seen.long_term] == [(2, "2")]


def test_looking_around_keeps_the_notes_and_remembering_keeps_the_springs() -> None:
    world, note = remember(WORLD, "a", "plan", "long_term")
    world, _ = begin_turn(world, "a")
    world, _ = begin_turn(world, "a")
    world, _ = remember(world, "a", "more", "short_term")
    memory = world.residents[0].memory
    assert memory.long_term == (note,)
    assert [sighting.spring_id for sighting in memory.known_springs] == ["near"]
    assert memory.activity_seen == world.activity.next_seq - 1


def test_forgetting_removes_a_note_of_either_term() -> None:
    world, keep = remember(WORLD, "a", "keep", "long_term")
    world, drop_long = remember(world, "a", "drop", "long_term")
    world, drop_short = remember(world, "a", "drop too", "short_term")
    world = forget(world, "a", drop_long.id)
    world = forget(world, "a", drop_short.id)

    _, seen = begin_turn(world, "a")

    assert seen.long_term == (keep,)
    assert seen.short_term == ()


def test_the_id_of_a_forgotten_note_is_not_used_again() -> None:
    world, _ = remember(WORLD, "a", "keep", "long_term")
    world, dropped = remember(world, "a", "drop", "long_term")
    world = forget(world, "a", dropped.id)

    _, again = remember(world, "a", "new", "long_term")

    assert again.id == 3


# 2 was forgotten already, 0 and 99 never existed.
@pytest.mark.parametrize("note_id", [0, 2, 99])
def test_forgetting_a_note_that_is_not_there_is_refused(note_id: int) -> None:
    world, _ = remember(WORLD, "a", "kept", "long_term")
    world, gone = remember(world, "a", "gone", "long_term")
    world = forget(world, "a", gone.id)
    with pytest.raises(NoteError) as error:
        forget(world, "a", note_id)
    assert error.value.code == "unknown_note"


def test_an_expired_note_cannot_be_forgotten_any_more() -> None:
    world, note = remember(WORLD, "a", "fleeting", "short_term")
    with pytest.raises(NoteError) as error:
        forget(at(world, note.expires_at or 0), "a", note.id)
    assert error.value.code == "unknown_note"


def test_another_residents_note_cannot_be_forgotten() -> None:
    world, note = remember(WORLD, "b", "harvest", "long_term")
    with pytest.raises(NoteError):
        forget(world, "a", note.id)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty_note"),
        ("  \n\t ", "empty_note"),
        ("x" * (NOTE_LENGTH_LIMIT + 1), "note_too_long"),
    ],
)
def test_empty_or_long_notes_are_refused(text: str, code: str) -> None:
    with pytest.raises(NoteError) as error:
        remember(WORLD, "a", text, "long_term")
    assert error.value.code == code


def test_a_note_may_fill_the_length_limit_once_trimmed() -> None:
    _, note = remember(WORLD, "a", f" {'x' * NOTE_LENGTH_LIMIT} ", "short_term")
    assert len(note.text) == NOTE_LENGTH_LIMIT


def full(term: NoteTerm) -> World:
    """a's notes of that term are at the limit."""
    world = WORLD
    for i in range(NOTE_LIMITS[term]):
        world, _ = remember(world, "a", f"note {i}", term)
    return world


@pytest.mark.parametrize("term", ["long_term", "short_term"])
def test_a_full_memory_refuses_another_note_of_that_term(term: NoteTerm) -> None:
    with pytest.raises(NoteError) as error:
        remember(full(term), "a", "one too many", term)

    assert error.value.code == "memory_full"


@pytest.mark.parametrize(
    ("term", "other"), [("long_term", "short_term"), ("short_term", "long_term")]
)
def test_a_full_memory_still_has_room_for_the_other_term(term: NoteTerm, other: NoteTerm) -> None:
    _, note = remember(full(term), "a", "the other term has room", other)

    assert note.text == "the other term has room"


@pytest.mark.parametrize("term", ["long_term", "short_term"])
def test_forgetting_makes_room_in_a_full_memory(term: NoteTerm) -> None:
    world = forget(full(term), "a", 1)

    _, note = remember(world, "a", "room again", term)

    assert note.text == "room again"


def test_expired_notes_make_room() -> None:
    world = WORLD
    for i in range(NOTE_LIMITS["short_term"]):
        world, _ = remember(world, "a", f"note {i}", "short_term")
    world, note = remember(at(world, 5.0 + SHORT_TERM_SECONDS), "a", "fresh", "short_term")
    assert world.residents[0].memory.short_term == (note,)


@pytest.mark.parametrize(
    ("text", "term"), [("x", "forever"), ("x", ["long_term"]), (None, "long_term")]
)
def test_an_unknown_term_or_text_that_is_not_a_string_is_a_programming_error(
    text: Any, term: Any
) -> None:
    with pytest.raises(ValueError):
        remember(WORLD, "a", text, term)


@pytest.mark.parametrize("note_id", [True, 1.0, "1"])
def test_only_an_integer_id_forgets_a_note(note_id: Any) -> None:
    world, _ = remember(WORLD, "a", "kept", "long_term")
    with pytest.raises(NoteError) as error:
        forget(world, "a", note_id)
    assert error.value.code == "unknown_note"


@pytest.mark.parametrize(
    ("busy", "resident_id"),
    [
        (perform(WORLD, MoveTo("b", Position(80, 20))), "b"),
        (perform(WORLD, Harvest("a", "near")), "a"),
    ],
    ids=["walking", "harvesting"],
)
def test_busy_residents_can_still_keep_notes(busy: World, resident_id: str) -> None:
    _, note = remember(busy, resident_id, "busy", "long_term")

    assert note.text == "busy"


@pytest.mark.parametrize(
    "keep",
    [
        lambda world, resident_id: remember(world, resident_id, "x", "long_term"),
        lambda world, resident_id: forget(world, resident_id, 1),
    ],
    ids=["remember", "forget"],
)
@pytest.mark.parametrize(
    ("world", "resident_id", "code"),
    [
        (WORLD, "nobody", "unknown_resident"),
        (perform(WORLD, LeaveGame("a")), "a", "resident_left"),
        (end_game(WORLD), "a", "game_over"),
    ],
)
def test_residents_who_cannot_act_cannot_keep_notes(
    keep: Callable[[World, str], object], world: World, resident_id: str, code: str
) -> None:
    with pytest.raises(GameError) as error:
        keep(world, resident_id)

    assert error.value.code == code


def test_notes_through_the_game_are_timed_from_now() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), clock=lambda: now[0])

    now[0] = 30.0
    note = game.remember("a", "fleeting", "short_term")
    now[0] = 30.0 + SHORT_TERM_SECONDS
    seen = game.begin_turn("a")

    assert note.written_at == 30.0
    assert note.expires_at == 30.0 + SHORT_TERM_SECONDS
    assert seen.short_term == ()


def test_notes_are_never_sent_to_pages() -> None:
    game = Game(WORLD)
    watcher = Outbox()
    game.watch(watcher)

    kept = game.remember("a", "keep", "long_term")
    game.forget("a", kept.id)

    assert watcher.state is None


def note(note_id: int, expires_at: float | None = None, text: str = "x") -> Note:
    return Note(note_id, text, 1.0, expires_at)


@pytest.mark.parametrize(
    "fields",
    [
        {"long_term": (note(1), note(1)), "next_note_id": 2},
        {"long_term": (note(2),), "next_note_id": 2},
        {"long_term": (note(0),), "next_note_id": 2},
        {"long_term": (note(1, expires_at=5.0),), "next_note_id": 2},
        {"short_term": (note(1),), "next_note_id": 2},
        {"short_term": (note(1, expires_at=1.0),), "next_note_id": 2},
        {"long_term": (note(1, text=" "),), "next_note_id": 2},
        {"long_term": (note(1, text="x" * (NOTE_LENGTH_LIMIT + 1)),), "next_note_id": 2},
        {
            "long_term": tuple(note(i) for i in range(1, NOTE_LIMITS["long_term"] + 2)),
            "next_note_id": NOTE_LIMITS["long_term"] + 2,
        },
        {
            "short_term": tuple(note(i, 9.0) for i in range(1, NOTE_LIMITS["short_term"] + 2)),
            "next_note_id": NOTE_LIMITS["short_term"] + 2,
        },
        {"long_term": (Note(1, "x", float("nan"), None),), "next_note_id": 2},
        {"short_term": (note(1, expires_at=float("inf")),), "next_note_id": 2},
        {"next_note_id": 0},
    ],
)
def test_memory_refuses_inconsistent_notes(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Memory(**fields)


def test_memory_orders_notes_by_id() -> None:
    memory = Memory(long_term=(note(3), note(1)), short_term=(note(2, 9.0),), next_note_id=4)
    assert [n.id for n in memory.long_term] == [1, 3]
