from dataclasses import replace
from typing import Any

import pytest

from aiverse_server.actions import LeaveGame, Mine, MoveTo, perform
from aiverse_server.app import Game, Outbox
from aiverse_server.lifecycle import GameError, end_game
from aiverse_server.memory import SHORT_TERM_SECONDS, NoteError, forget, remember
from aiverse_server.perception import begin_turn
from aiverse_server.tokens import TOKEN
from aiverse_server.world import (
    NOTE_LENGTH_LIMIT,
    NOTE_LIMITS,
    Inventory,
    Memory,
    Note,
    NoteTerm,
    Pool,
    Position,
    Resident,
    World,
    WorldMap,
)


def resident(resident_id: str, x: float, y: float) -> Resident:
    return Resident(
        id=resident_id,
        name=resident_id.upper(),
        color="#123456",
        position=Position(x, y),
        inventory=Inventory(((TOKEN, 1000), ("MK1", 1))),
    )


WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a", 10, 10), resident("b", 80, 80)),
    pools=(Pool("near", Position(12, 10), radius=4, reserve=100),),
    time=5.0,
)


def at(world: World, time: float) -> World:
    return replace(world, time=time)


def test_the_resident_chooses_how_long_each_note_lasts() -> None:
    world, plan = remember(WORLD, "a", "  sell tokens to b above 3 coins  ", "long_term")
    world, sight = remember(world, "a", "b is heading north", "short_term")
    assert plan == Note(1, "sell tokens to b above 3 coins", 5.0, None)
    assert sight == Note(2, "b is heading north", 5.0, 5.0 + SHORT_TERM_SECONDS)
    _, seen = begin_turn(world, "a")
    assert seen.long_term == (plan,)
    assert seen.short_term == (sight,)
    # Nobody else's memory changes.
    assert world.residents[1].memory == Memory()


def test_short_term_notes_expire_and_long_term_notes_last_the_game() -> None:
    world, _ = remember(WORLD, "a", "keep", "long_term")
    world, _ = remember(world, "a", "fleeting", "short_term")
    expires = 5.0 + SHORT_TERM_SECONDS
    world, before = begin_turn(at(world, expires - 0.01), "a")
    assert [note.text for note in before.short_term] == ["fleeting"]
    world, after = begin_turn(at(world, expires), "a")
    assert after.short_term == ()
    assert world.residents[0].memory.short_term == ()
    world, much_later = begin_turn(at(world, 1000.0), "a")
    assert [note.text for note in much_later.long_term] == ["keep"]


def test_notes_come_back_oldest_first_with_ids_shared_across_terms() -> None:
    world = WORLD
    for text, term in (("1", "short_term"), ("2", "long_term"), ("3", "short_term")):
        world, _ = remember(world, "a", text, term)  # type: ignore[arg-type]
    _, seen = begin_turn(world, "a")
    assert [(note.id, note.text) for note in seen.short_term] == [(1, "1"), (3, "3")]
    assert [(note.id, note.text) for note in seen.long_term] == [(2, "2")]


def test_looking_around_keeps_the_notes_and_remembering_keeps_the_pools() -> None:
    world, note = remember(WORLD, "a", "plan", "long_term")
    world, _ = begin_turn(world, "a")
    world, _ = begin_turn(world, "a")
    world, _ = remember(world, "a", "more", "short_term")
    memory = world.residents[0].memory
    assert memory.long_term == (note,)
    assert [sighting.pool_id for sighting in memory.known_pools] == ["near"]
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
    # Ids are never reused.
    _, again = remember(world, "a", "new", "long_term")
    assert again.id == 4


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
    world, note = remember(WORLD, "b", "mine", "long_term")
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


@pytest.mark.parametrize("term", ["long_term", "short_term"])
def test_a_full_memory_refuses_until_the_resident_forgets(term: NoteTerm) -> None:
    world = WORLD
    for i in range(NOTE_LIMITS[term]):
        world, _ = remember(world, "a", f"note {i}", term)
    with pytest.raises(NoteError) as error:
        remember(world, "a", "one too many", term)
    assert error.value.code == "memory_full"
    other: NoteTerm = "short_term" if term == "long_term" else "long_term"
    remember(world, "a", "the other term has room", other)
    world = forget(world, "a", 1)
    remember(world, "a", "room again", term)


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


def test_busy_residents_can_still_keep_notes() -> None:
    walking = perform(WORLD, MoveTo("b", Position(80, 20)))
    remember(walking, "b", "walking north", "short_term")
    mining = perform(WORLD, Mine("a", "near"))
    remember(mining, "a", "mining near", "long_term")


@pytest.mark.parametrize(
    ("world", "resident_id", "code"),
    [
        (WORLD, "nobody", "unknown_resident"),
        (perform(WORLD, LeaveGame("a")), "a", "resident_left"),
        (end_game(WORLD), "a", "game_over"),
    ],
)
def test_residents_who_cannot_act_cannot_keep_notes(
    world: World, resident_id: str, code: str
) -> None:
    with pytest.raises(GameError) as error:
        remember(world, resident_id, "x", "long_term")
    assert error.value.code == code
    with pytest.raises(GameError):
        forget(world, resident_id, 1)


def test_notes_are_timed_from_now_through_the_game_and_never_sent() -> None:
    now = [0.0]
    game = Game(replace(WORLD, time=0.0), clock=lambda: now[0])
    watcher = Outbox()
    game.watch(watcher)
    now[0] = 30.0
    note = game.remember("a", "fleeting", "short_term")
    assert note.written_at == 30.0
    assert note.expires_at == 30.0 + SHORT_TERM_SECONDS
    kept = game.remember("a", "keep", "long_term")
    game.forget("a", kept.id)
    now[0] = 30.0 + SHORT_TERM_SECONDS
    assert game.begin_turn("a").short_term == ()
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
