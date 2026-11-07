"""Speech: residents talking to each other, with nothing traded.

A resident either speaks aloud or to one other resident:

- aloud, they are heard by every other resident still in the game within `HEARING_RADIUS`
  of them, walkers where they have got to on their way;
- to one resident, only that resident hears, wherever they are.

Who hears is settled the moment the words are spoken and kept with them, so a resident who
walks up afterwards has missed them. Each listener, and the speaker, gets the words in their
next perception, see `perception.begin_turn`, which is how the AI will hear them.

Each line costs compute from those the speaker carries, `SPEECH_COSTS`: more aloud, since more
can hear it, than to one resident. A line that spends the last compute takes the speaker out of
the game like any other spending, see `lifecycle.knock_out_exhausted`.

Speaking goes through the primitive `actions.Say`, which works while walking or harvesting. The
world keeps the latest `SPEECH_LIMIT` lines, so a resident who looks
rarely may miss the oldest. Pages see every line spoken aloud, and only the whispers to or
from the resident they view as, see `protocol.world_view`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from dataclasses import replace
from typing import Literal

from ..core.world import SPEECH_LENGTH_LIMIT, SPEECH_LIMIT, Position, Speech, SpeechLog, World
from ..economy.compute import pay_compute
from ..game.lifecycle import check_can_act
from .perception import PERCEPTION_RADIUS
from .walking import position_at

HEARING_RADIUS = PERCEPTION_RADIUS
"""How far, in map units, words spoken aloud carry: as far as a resident notices things."""

SPEECH_COSTS = {"aloud": 2, "to_one": 1}
"""Compute one line costs, spoken aloud or to one resident."""

SpeechErrorCode = Literal["empty_speech", "speech_too_long", "invalid_speech", "unknown_listener"]


class SpeechError(Exception):
    def __init__(self, code: SpeechErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: SpeechErrorCode = code
        self.message = message


def say(world: World, resident_id: str, text: str, to: str | None = None) -> World:
    """Speak `text` aloud, or to the resident `to` alone, paying `SPEECH_COSTS` from the
    compute the speaker carries. Surrounding whitespace is trimmed. Refused with `GameError`
    for a speaker who cannot act, `ComputeError` for one who carries too little compute, and with
    `SpeechError` for empty or
    over-long words, words that cannot be sent as UTF-8, or a listener who does not exist, has
    left or is the speaker. Text that is not a string is the caller's mistake and raises
    `ValueError`."""
    if not isinstance(text, str):
        raise ValueError(f"speech text must be a string, got {type(text).__name__}")
    check_can_act(world, resident_id)
    text = text.strip()
    if not text:
        raise SpeechError("empty_speech", "say something")
    if len(text) > SPEECH_LENGTH_LIMIT:
        raise SpeechError(
            "speech_too_long",
            f"one line holds at most {SPEECH_LENGTH_LIMIT} characters, got {len(text)}",
        )
    # A lone surrogate, which JSON can carry as an escape, would make every later state that
    # includes this line impossible to send.
    if not is_sendable(text):
        raise SpeechError("invalid_speech", "text must be valid Unicode, without lone surrogates")
    me = next(resident for resident in world.residents if resident.id == resident_id)
    here = position_at(me, world.time)
    if to is not None:
        listener = next((resident for resident in world.residents if resident.id == to), None)
        if listener is None or listener.left or to == resident_id:
            raise SpeechError(
                "unknown_listener", f"{to!r} is not another resident still in the game"
            )
        listeners: tuple[str, ...] = (to,)
    else:
        listeners = tuple(
            other.id
            for other in world.residents
            if other.id != resident_id
            and not other.left
            and _within_earshot(here, position_at(other, world.time))
        )
    cost = SPEECH_COSTS["aloud" if to is None else "to_one"]
    world = pay_compute(world, resident_id, cost, "saying that")
    log = world.speech
    line = Speech(log.next_seq, world.time, resident_id, text, here, to, listeners)
    entries = (*log.entries, line)[-SPEECH_LIMIT:]
    return replace(world, speech=SpeechLog(entries, log.next_seq + 1))


def is_sendable(text: str) -> bool:
    """Whether `text` can go out in a JSON text frame, that is, be encoded as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _within_earshot(here: Position, there: Position) -> bool:
    return math.dist((here.x, here.y), (there.x, there.y)) <= HEARING_RADIUS
