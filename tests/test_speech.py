from dataclasses import replace

import pytest

from aiverse_server.actions import ActionRejected, LeaveGame, Mine, MoveTo, Say, perform
from aiverse_server.lifecycle import end_game
from aiverse_server.perception import begin_turn
from aiverse_server.speech import HEARING_RADIUS, SpeechError, say
from aiverse_server.tokens import TOKEN
from aiverse_server.world import (
    SPEECH_LENGTH_LIMIT,
    SPEECH_LIMIT,
    Inventory,
    Memory,
    Pool,
    Position,
    Resident,
    Speech,
    SpeechLog,
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


# a speaks from (10, 10): b is close, d right on the edge of earshot, c far away.
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        resident("a", 10, 10),
        resident("b", 22, 10),
        resident("c", 80, 80),
        resident("d", 10, 10 + HEARING_RADIUS),
    ),
    pools=(Pool("p", Position(10, 10), radius=4, reserve=100),),
    time=4.0,
)


def heard(world: World, resident_id: str) -> tuple[World, list[tuple[str, str]]]:
    """Looks around and returns (speaker, words) of what came to the resident's ears."""
    world, seen = begin_turn(world, resident_id)
    return world, [(line.speaker_id, line.text) for line in seen.speech]


def test_words_spoken_aloud_reach_everyone_in_earshot_and_no_further() -> None:
    world = perform(WORLD, Say("a", "  anyone selling tokens?  "))
    line = world.speech.entries[0]
    assert line == Speech(1, 4.0, "a", "anyone selling tokens?", Position(10, 10), None, ("b", "d"))
    for listener in ("b", "d"):
        assert heard(world, listener)[1] == [("a", "anyone selling tokens?")]
    assert heard(world, "c")[1] == []


def test_a_whisper_reaches_only_its_listener_wherever_they_are() -> None:
    world = perform(WORLD, Say("a", "meet me at the pool", to="c"))
    assert world.speech.entries[0].listeners == ("c",)
    assert heard(world, "c")[1] == [("a", "meet me at the pool")]
    # b stands right next to a and still hears nothing.
    assert heard(world, "b")[1] == []


def test_a_conversation_comes_back_in_order_with_the_residents_own_words() -> None:
    world = perform(WORLD, Say("a", "hi b", to="b"))
    world = perform(world, Say("b", "hi a", to="a"))
    world = perform(world, Say("d", "hello all"))
    _, lines = heard(world, "a")
    assert lines == [("a", "hi b"), ("b", "hi a"), ("d", "hello all")]


def test_each_line_is_heard_once() -> None:
    world = perform(WORLD, Say("a", "once"))
    world, first = heard(world, "b")
    world, second = heard(world, "b")
    assert (first, second) == ([("a", "once")], [])
    world = perform(world, Say("a", "twice"))
    assert heard(world, "b")[1] == [("a", "twice")]


def test_who_hears_is_settled_when_it_is_said() -> None:
    world = perform(WORLD, Say("a", "before you came"))
    # c walks up afterwards and has missed it; b walks away and still heard it.
    world = replace(
        world,
        residents=tuple(
            replace(r, position=Position(11, 11))
            if r.id == "c"
            else replace(r, position=Position(90, 90))
            if r.id == "b"
            else r
            for r in world.residents
        ),
    )
    assert heard(world, "c")[1] == []
    assert heard(world, "b")[1] == [("a", "before you came")]


def test_walkers_hear_and_speak_where_they_have_got_to() -> None:
    # c walks from (80, 80) straight to (10, 20), coming within earshot of a near the end.
    world = perform(WORLD, MoveTo("c", Position(10, 20)))
    walk = next(r.walking for r in world.residents if r.id == "c")
    assert walk is not None
    early = replace(world, time=walk.departs_at + 1)
    assert "c" not in say(early, "a", "too far").speech.entries[-1].listeners
    late = replace(world, time=walk.arrives_at - 1)
    assert "c" in say(late, "a", "close now").speech.entries[-1].listeners
    spoken = say(late, "c", "on my way").speech.entries[-1]
    assert spoken.position != Position(80, 80)
    assert spoken.position != Position(10, 20)
    assert "a" in spoken.listeners


def test_busy_residents_can_still_talk() -> None:
    mining = perform(WORLD, Mine("a", "p"))
    assert perform(mining, Say("a", "digging")).speech.entries[-1].text == "digging"
    walking = perform(WORLD, MoveTo("b", Position(50, 50)))
    assert perform(walking, Say("b", "walking", to="c")).speech.entries[-1].to == "c"


def test_residents_who_left_neither_speak_nor_hear() -> None:
    world = perform(WORLD, LeaveGame("b"))
    assert world.speech.entries == ()
    assert perform(world, Say("a", "hello")).speech.entries[-1].listeners == ("d",)
    with pytest.raises(ActionRejected) as refused:
        perform(world, Say("b", "let me back"))
    assert refused.value.code == "resident_left"
    with pytest.raises(ActionRejected) as refused:
        perform(world, Say("a", "are you there?", to="b"))
    assert refused.value.code == "unknown_listener"


@pytest.mark.parametrize(
    ("text", "to", "code"),
    [
        ("   ", None, "empty_speech"),
        ("x" * (SPEECH_LENGTH_LIMIT + 1), None, "speech_too_long"),
        ("talking to myself", "a", "unknown_listener"),
        ("hello?", "nobody", "unknown_listener"),
        ("hi \ud800", None, "invalid_speech"),
    ],
)
def test_bad_speech_is_refused_and_changes_nothing(text: str, to: str | None, code: str) -> None:
    with pytest.raises(ActionRejected) as refused:
        perform(WORLD, Say("a", text, to))
    assert refused.value.code == code


def test_a_full_line_fits_after_trimming() -> None:
    text = " " + "字" * SPEECH_LENGTH_LIMIT + " "
    assert say(WORLD, "a", text).speech.entries[0].text == "字" * SPEECH_LENGTH_LIMIT


def test_nobody_speaks_once_the_game_is_over() -> None:
    with pytest.raises(ActionRejected) as refused:
        perform(end_game(WORLD), Say("a", "gg"))
    assert refused.value.code == "game_over"


def test_text_that_is_not_a_string_is_the_callers_mistake() -> None:
    with pytest.raises(ValueError):
        say(WORLD, "a", 42)  # type: ignore[arg-type]
    with pytest.raises(SpeechError):
        say(WORLD, "a", "")


def test_the_log_keeps_only_the_latest_lines_and_numbering_goes_on() -> None:
    world = WORLD
    for i in range(SPEECH_LIMIT + 5):
        world = say(world, "a", str(i))
    entries = world.speech.entries
    assert len(entries) == SPEECH_LIMIT
    assert (entries[0].seq, entries[-1].seq, world.speech.next_seq) == (6, SPEECH_LIMIT + 5, 206)
    # b never looked, so only what is still kept reaches them.
    _, lines = heard(world, "b")
    assert len(lines) == SPEECH_LIMIT


def test_speaking_changes_nothing_on_the_map() -> None:
    world = perform(WORLD, Say("a", "just talking"))
    assert world.residents == WORLD.residents
    assert world.activity == WORLD.activity


def test_a_world_rejects_speech_it_cannot_account_for() -> None:
    line = Speech(1, 0.0, "a", "hi", Position(0, 0), None, ("b",))
    with pytest.raises(ValueError):
        replace(WORLD, speech=SpeechLog((replace(line, speaker_id="ghost"),), 2))
    with pytest.raises(ValueError):
        replace(WORLD, speech=SpeechLog((replace(line, listeners=("ghost",)),), 2))
    with pytest.raises(ValueError):
        SpeechLog((replace(line, listeners=("a",)),), 2)
    with pytest.raises(ValueError):
        SpeechLog((replace(line, to="b", listeners=("b", "c")),), 2)
    with pytest.raises(ValueError):
        SpeechLog((replace(line, text=" "),), 2)
    with pytest.raises(ValueError):
        SpeechLog((replace(line, text="hi \udfff"),), 2)
    with pytest.raises(ValueError):
        SpeechLog((line,), 1)
    resident_a = replace(WORLD.residents[0], memory=Memory(speech_seen=1))
    with pytest.raises(ValueError):
        replace(WORLD, residents=(resident_a, *WORLD.residents[1:]))
