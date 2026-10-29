"""Memory a resident keeps by choice: long-term and short-term notes.

Perception (`perception.py`) remembers some things on its own, such as the pools a resident
has seen. Anything else is up to whoever drives the resident, such as the AI that will: it
writes down what it wants to keep as a note with `remember`, and chooses how long it lasts:

- a long-term note stays for the rest of the game, until the resident forgets it;
- a short-term note is cleared `SHORT_TERM_SECONDS` of game time after it is written.

A note is plain text of at most `NOTE_LENGTH_LIMIT` characters. A resident keeps at most
`NOTE_LIMITS` notes of each term; once full, `remember` is refused until they `forget` one,
so deciding what is worth keeping stays with the resident. Every note is handed back each
round in the resident's perception, see `perception.begin_turn`.

Keeping notes is not a primitive (`actions.py`): it changes nothing on the map, costs no
tokens, works while mining or walking, and is not broadcast, since memory is not in the
protocol. Like perception, it is refused for a resident who does not exist or has left, and
once the game is over. Notes live in the in-memory world, so a new game starts without any.

Expired notes are cleared whenever the resident's memory is used (`remember`, `forget` and
`begin_turn`), so a resident never sees one; until then they may linger in the world.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.world import NOTE_LENGTH_LIMIT, NOTE_LIMITS, Memory, Note, NoteTerm, World
from ..game.lifecycle import check_can_act

SHORT_TERM_SECONDS = 120.0
"""Game seconds a short-term note lasts: about six rounds of the design document's 20 s."""

NoteErrorCode = Literal["empty_note", "note_too_long", "memory_full", "unknown_note"]


class NoteError(Exception):
    def __init__(self, code: NoteErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: NoteErrorCode = code
        self.message = message


def remember(world: World, resident_id: str, text: str, term: NoteTerm) -> tuple[World, Note]:
    """Write a note into the resident's memory, lasting the whole game (`"long_term"`) or
    `SHORT_TERM_SECONDS` (`"short_term"`). Surrounding whitespace is trimmed. Returns the new
    world and the note, whose id `forget` takes. A term that is neither, or text that is not a
    string, is the caller's mistake and raises `ValueError`."""
    # Compared against a tuple rather than looked up, so an unhashable term cannot slip by
    # as a TypeError.
    if term not in ("long_term", "short_term"):
        raise ValueError(f"note term must be long_term or short_term, got {term!r}")
    if not isinstance(text, str):
        raise ValueError(f"note text must be a string, got {type(text).__name__}")
    check_can_act(world, resident_id)
    text = text.strip()
    if not text:
        raise NoteError("empty_note", "a note needs some text")
    if len(text) > NOTE_LENGTH_LIMIT:
        raise NoteError(
            "note_too_long", f"a note holds at most {NOTE_LENGTH_LIMIT} characters, got {len(text)}"
        )
    memory = _current_memory(world, resident_id)
    notes = memory.long_term if term == "long_term" else memory.short_term
    if len(notes) >= NOTE_LIMITS[term]:
        raise NoteError(
            "memory_full",
            f"{term} memory already holds {NOTE_LIMITS[term]} notes; forget one first",
        )
    next_id = memory.next_note_id + 1
    if term == "long_term":
        note = Note(memory.next_note_id, text, world.time, None)
        memory = replace(memory, long_term=(*notes, note), next_note_id=next_id)
    else:
        note = Note(memory.next_note_id, text, world.time, world.time + SHORT_TERM_SECONDS)
        memory = replace(memory, short_term=(*notes, note), next_note_id=next_id)
    return _with_memory(world, resident_id, memory), note


def forget(world: World, resident_id: str, note_id: int) -> World:
    """Remove a note of either term from the resident's memory. A note that has already
    expired is gone and refused like one that never existed, and so is an id that is not an
    integer: `True` or `1.0` would otherwise compare equal to note 1."""
    check_can_act(world, resident_id)
    if type(note_id) is not int:
        raise NoteError("unknown_note", f"note id must be an integer, got {note_id!r}")
    memory = _current_memory(world, resident_id)
    long_term = tuple(note for note in memory.long_term if note.id != note_id)
    short_term = tuple(note for note in memory.short_term if note.id != note_id)
    if len(long_term) + len(short_term) == len(memory.long_term) + len(memory.short_term):
        raise NoteError("unknown_note", f"resident {resident_id!r} has no note {note_id!r}")
    memory = replace(memory, long_term=long_term, short_term=short_term)
    return _with_memory(world, resident_id, memory)


def forget_expired(memory: Memory, time: float) -> Memory:
    """The memory without the short-term notes expired by game time `time`; `memory` itself
    when none has."""
    kept = tuple(
        note for note in memory.short_term if note.expires_at is None or time < note.expires_at
    )
    if len(kept) == len(memory.short_term):
        return memory
    return replace(memory, short_term=kept)


def _current_memory(world: World, resident_id: str) -> Memory:
    me = next(resident for resident in world.residents if resident.id == resident_id)
    return forget_expired(me.memory, world.time)


def _with_memory(world: World, resident_id: str, memory: Memory) -> World:
    residents = tuple(
        replace(resident, memory=memory) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
