from collections.abc import Callable
from dataclasses import replace

import pytest

from aiverse_server.core.world import (
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
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import (
    ActionRejected,
    LeaveGame,
    Mine,
    MoveTo,
    OfferToSell,
    Say,
    perform,
)
from aiverse_server.game.lifecycle import end_game
from aiverse_server.residents.perception import begin_turn
from aiverse_server.residents.speech import HEARING_RADIUS, SPEECH_COSTS, SpeechError, say


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
    world = perform(world, Say("a", "twice"))
    _, third = heard(world, "b")

    assert (first, second, third) == ([("a", "once")], [], [("a", "twice")])


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
    late = replace(world, time=walk.arrives_at - 1)

    too_far = say(early, "a", "too far").speech.entries[-1]
    close_now = say(late, "a", "close now").speech.entries[-1]
    spoken = say(late, "c", "on my way").speech.entries[-1]

    assert "c" not in too_far.listeners
    assert "c" in close_now.listeners
    assert spoken.position != Position(80, 80)
    assert spoken.position != Position(10, 20)
    assert "a" in spoken.listeners


@pytest.mark.parametrize(
    ("busy", "line"),
    [
        (perform(WORLD, Mine("a", "p")), Say("a", "digging")),
        (perform(WORLD, MoveTo("b", Position(50, 50))), Say("b", "walking", to="c")),
    ],
    ids=["mining", "walking"],
)
def test_busy_residents_can_still_talk(busy: World, line: Say) -> None:
    spoken = perform(busy, line).speech.entries[-1]

    assert (spoken.speaker_id, spoken.text, spoken.to) == (line.resident_id, line.text, line.to)


B_LEFT = perform(WORLD, LeaveGame("b"))


def test_residents_who_left_hear_nothing_said_aloud() -> None:
    assert perform(B_LEFT, Say("a", "hello")).speech.entries[-1].listeners == ("d",)


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (Say("b", "let me back"), "resident_left"),
        (Say("a", "are you there?", to="b"), "unknown_listener"),
    ],
    ids=["speaking", "being whispered to"],
)
def test_residents_who_left_neither_speak_nor_are_spoken_to(line: Say, code: str) -> None:
    with pytest.raises(ActionRejected) as refused:
        perform(B_LEFT, line)

    assert refused.value.code == code


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
    with pytest.raises(ValueError, match="must be a string"):
        say(WORLD, "a", 42)  # type: ignore[arg-type]


def test_empty_text_is_refused_as_speech_not_as_a_mistake() -> None:
    with pytest.raises(SpeechError) as refused:
        say(WORLD, "a", "")

    assert refused.value.code == "empty_speech"


def test_the_log_keeps_only_the_latest_lines_and_numbering_goes_on() -> None:
    world = WORLD
    for i in range(SPEECH_LIMIT + 5):
        world = say(world, "a", str(i))

    _, lines = heard(world, "b")

    entries = world.speech.entries
    assert len(entries) == SPEECH_LIMIT
    assert (entries[0].seq, entries[-1].seq, world.speech.next_seq) == (6, SPEECH_LIMIT + 5, 206)
    # b never looked, so only what is still kept reaches them.
    assert len(lines) == SPEECH_LIMIT


def tokens(world: World, resident_id: str) -> int:
    return next(r for r in world.residents if r.id == resident_id).inventory.count(TOKEN)


def with_tokens(world: World, resident_id: str, count: int) -> World:
    items = Inventory(((TOKEN, count),)) if count else Inventory()
    residents = tuple(
        replace(r, inventory=items) if r.id == resident_id else r for r in world.residents
    )
    return replace(world, residents=residents)


def test_speaking_aloud_costs_more_than_a_whisper() -> None:
    aloud = perform(WORLD, Say("a", "hello all"))
    whisper = perform(WORLD, Say("a", "hello b", to="b"))
    assert tokens(aloud, "a") == 1000 - SPEECH_COSTS["aloud"]
    assert tokens(whisper, "a") == 1000 - SPEECH_COSTS["to_one"]
    assert SPEECH_COSTS == {"aloud": 2, "to_one": 1}


def test_speaking_moves_nothing_on_the_map() -> None:
    world = perform(WORLD, Say("a", "just talking"))
    assert [r.position for r in world.residents] == [r.position for r in WORLD.residents]
    assert world.activity == WORLD.activity


def test_too_few_tokens_to_speak_is_refused_and_changes_nothing() -> None:
    world = with_tokens(WORLD, "a", 1)

    with pytest.raises(ActionRejected) as refused:
        perform(world, Say("a", "hello all"))

    assert refused.value.code == "not_enough_tokens"


def test_whispering_with_the_last_token_takes_the_speaker_out() -> None:
    world = with_tokens(WORLD, "a", 1)

    after = perform(world, Say("a", "goodbye", to="b"))

    assert next(r for r in after.residents if r.id == "a").left
    assert after.speech.entries[-1].text == "goodbye"


def test_tokens_set_aside_in_an_offer_do_not_pay_for_speech() -> None:
    world = with_tokens(WORLD, "a", 2)
    world = perform(world, OfferToSell("a", "b", TOKEN, 1, 10))
    with pytest.raises(ActionRejected) as refused:
        perform(world, Say("a", "hello all"))
    assert refused.value.code == "not_enough_tokens"


LINE = Speech(1, 0.0, "a", "hi", Position(0, 0), None, ("b",))


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: replace(WORLD, speech=SpeechLog((replace(LINE, speaker_id="ghost"),), 2)),
            "unknown resident",
        ),
        (
            lambda: replace(WORLD, speech=SpeechLog((replace(LINE, listeners=("ghost",)),), 2)),
            "unknown resident",
        ),
        (lambda: SpeechLog((replace(LINE, listeners=("a",)),), 2), "speaker or a listener twice"),
        (lambda: SpeechLog((replace(LINE, to="b", listeners=("b", "c")),), 2), "heard by others"),
        (lambda: SpeechLog((replace(LINE, text=" "),), 2), "empty or too long"),
        (lambda: SpeechLog((replace(LINE, text="hi \udfff"),), 2), "cannot be encoded"),
        (lambda: SpeechLog((LINE,), 1), "must be an integer above 0"),
        (
            lambda: replace(
                WORLD,
                residents=(
                    replace(WORLD.residents[0], memory=Memory(speech_seen=1)),
                    *WORLD.residents[1:],
                ),
            ),
            "heard speech not yet said",
        ),
    ],
    ids=[
        "unknown speaker",
        "unknown listener",
        "speaker listening",
        "whisper overheard",
        "blank",
        "lone surrogate",
        "seq not issued",
        "heard before said",
    ],
)
def test_a_world_rejects_speech_it_cannot_account_for(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()
