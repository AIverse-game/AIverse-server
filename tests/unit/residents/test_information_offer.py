from dataclasses import replace

import pytest

from aiverse_server.core.events import pending_for, reject_event, withdraw_event
from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.world import (
    NOTE_LENGTH_LIMIT,
    NOTE_LIMITS,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.game.actions import ActionRejected, OfferInformation, RespondToEvent, perform
from aiverse_server.game.lifecycle import end_game, leave_game
from aiverse_server.residents.information_offer import MAX_PENDING_PER_PAIR, SUMMARY_LIMIT
from aiverse_server.residents.memory import forget, remember

# s knows something and has no coins; b and c can pay for it.
WORLD = World(
    map=WorldMap(size=10),
    residents=(
        Resident(id="s", name="s", color="#ffffff", position=Position(1, 1), coins=0),
        Resident(id="b", name="b", color="#000000", position=Position(2, 2), coins=50),
        Resident(id="c", name="c", color="#888888", position=Position(3, 3), coins=50),
    ),
)

SUMMARY = "a rich spring"
CONTENT = "spring p3 near (40, 90) held 900 energy"


def sell(world: World, buyer_id: str = "b", price: int = 30) -> World:
    return perform(world, OfferInformation("s", buyer_id, SUMMARY, CONTENT, price))


def accept(world: World, buyer_id: str = "b") -> World:
    event = pending_for(world, buyer_id)[0]
    return perform(world, RespondToEvent(buyer_id, event.id, True))


def notes(world: World, resident_id: str) -> tuple[str, ...]:
    me = next(resident for resident in world.residents if resident.id == resident_id)
    return tuple(note.text for note in me.memory.long_term)


def full_memory(world: World, resident_id: str) -> World:
    for i in range(NOTE_LIMITS["long_term"]):
        world, _ = remember(world, resident_id, f"note {i}", "long_term")
    return world


def test_sending_an_offer_sets_nothing_aside() -> None:
    sent = sell(WORLD)

    assert sent.residents == WORLD.residents
    assert len(pending_for(sent, "b")) == 1


def test_accepting_pays_the_seller_and_gives_the_buyer_a_note_with_the_content() -> None:
    accepted = accept(sell(WORLD))

    assert coin_balance(accepted, "b") == 20
    assert coin_balance(accepted, "s") == 30
    [note] = notes(accepted, "b")
    assert CONTENT in note and "from s" in note and SUMMARY in note
    assert accepted.events.pending == ()


def test_the_seller_keeps_the_information_and_can_sell_it_again() -> None:
    world, _ = remember(WORLD, "s", CONTENT, "long_term")

    after = accept(sell(accept(sell(world, "b")), "c"), "c")

    assert notes(after, "s") == (CONTENT,)
    assert len(notes(after, "b")) == 1 and len(notes(after, "c")) == 1
    assert coin_balance(after, "s") == 60


def test_declining_changes_nothing() -> None:
    sent = sell(WORLD)
    event = pending_for(sent, "b")[0]

    rejected = reject_event(sent, "b", event.id)
    withdrawn = withdraw_event(sent, "s", event.id)

    assert rejected.residents == WORLD.residents and rejected.events.pending == ()
    assert withdrawn.residents == WORLD.residents and withdrawn.events.pending == ()


def test_a_buyer_short_of_coins_leaves_the_offer_pending() -> None:
    sent = sell(WORLD, price=51)

    with pytest.raises(ActionRejected) as raised:
        accept(sent)

    assert raised.value.code == "not_enough_coins"
    assert len(pending_for(sent, "b")) == 1


def test_a_buyer_with_full_memory_must_forget_a_note_first() -> None:
    sent = sell(full_memory(WORLD, "b"))

    with pytest.raises(ActionRejected) as raised:
        accept(sent)
    freed = forget(sent, "b", 1)
    accepted = accept(freed)

    assert raised.value.code == "memory_full"
    assert coin_balance(accepted, "b") == 20
    assert CONTENT in notes(accepted, "b")[-1]


@pytest.mark.parametrize(
    ("summary", "content", "price"),
    [
        ("", CONTENT, 30),
        ("   ", CONTENT, 30),
        (SUMMARY, "", 30),
        ("x" * (SUMMARY_LIMIT + 1), CONTENT, 30),
        (SUMMARY, "x" * NOTE_LENGTH_LIMIT, 30),
        (SUMMARY, CONTENT, 0),
        (SUMMARY, CONTENT, -5),
        (SUMMARY, CONTENT, True),
        (SUMMARY, CONTENT, 1.5),
        (SUMMARY, CONTENT, 10**9 + 1),
        (7, CONTENT, 30),
        (SUMMARY, None, 30),
    ],
)
def test_terms_that_cannot_make_an_offer_are_refused(
    summary: str, content: str, price: int
) -> None:
    with pytest.raises(ActionRejected) as raised:
        perform(WORLD, OfferInformation("s", "b", summary, content, price))

    assert raised.value.code == "invalid_information"


def test_an_offer_to_oneself_or_a_stranger_is_refused() -> None:
    with pytest.raises(ActionRejected) as to_self:
        perform(WORLD, OfferInformation("s", "s", SUMMARY, CONTENT, 5))
    with pytest.raises(ActionRejected) as to_stranger:
        perform(WORLD, OfferInformation("s", "zz", SUMMARY, CONTENT, 5))

    assert to_self.value.code == "self_addressed"
    assert to_stranger.value.code == "unknown_resident"


def test_the_buyers_note_is_never_longer_than_a_note_may_be() -> None:
    world = replace(WORLD)
    longest = "x" * (NOTE_LENGTH_LIMIT - len(f"Bought from s ({SUMMARY}): "))

    accepted = accept(perform(world, OfferInformation("s", "b", SUMMARY, longest, 5)))

    assert len(notes(accepted, "b")[0]) == NOTE_LENGTH_LIMIT


def test_a_seller_cannot_bury_one_buyer_in_offers() -> None:
    crowded = WORLD
    for _ in range(MAX_PENDING_PER_PAIR):
        crowded = sell(crowded)

    with pytest.raises(ActionRejected) as raised:
        sell(crowded)
    answered = sell(accept(crowded))
    elsewhere = sell(crowded, "c")

    assert raised.value.code == "too_many_offers"
    assert len(pending_for(answered, "b")) == MAX_PENDING_PER_PAIR
    assert len(pending_for(elsewhere, "c")) == 1


@pytest.mark.parametrize("leaver", ["s", "b"])
def test_a_resident_leaving_clears_pending_information_offers_and_moves_no_coins(
    leaver: str,
) -> None:
    sent = sell(WORLD)

    after = leave_game(sent, leaver)

    assert after.events.pending == ()
    assert [coin_balance(after, r) for r in "sbc"] == [0, 50, 50]


def test_the_game_ending_clears_pending_information_offers_and_moves_no_coins() -> None:
    after = end_game(sell(WORLD))

    assert after.events.pending == ()
    assert [coin_balance(after, r) for r in "sbc"] == [0, 50, 50]
