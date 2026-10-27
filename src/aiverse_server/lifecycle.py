"""A game's lifecycle: who may still act, residents leaving, the game ending, the ranking.

A game runs for a fixed time after the server creates its world, `GAME_DURATION` unless the
server is started with another; the clock itself lives with the server, see `app.py`. When
it runs out, `end_game` settles every pending event, returning what offers set aside, and
from then on nothing changes. Residents may leave earlier, of their own accord or because
the server makes them; the game goes on without them. They stay in the world and in the
ranking with the coins they had, but lose their items and can no longer act or trade.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from .events import Event, withdraw_event
from .inventory import clear_items
from .trade import reserved_coins
from .world import World

GAME_DURATION = 20 * 60.0
"""Seconds from the world's creation to the end of the game."""

GameErrorCode = Literal["unknown_resident", "resident_left", "game_over"]


class GameError(Exception):
    def __init__(self, code: GameErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: GameErrorCode = code
        self.message = message


def check_can_act(world: World, resident_id: str) -> None:
    """Refuse a resident who does not exist or has left, and everyone once the game is over."""
    if world.ended:
        raise GameError("game_over", "the game is over")
    for resident in world.residents:
        if resident.id == resident_id:
            if resident.left:
                raise GameError("resident_left", f"resident {resident_id!r} has left the game")
            return
    raise GameError("unknown_resident", f"no resident with id {resident_id!r}")


def leave_game(world: World, resident_id: str) -> World:
    """Take the resident out of the game: their pending events are declined, which hands
    back whatever offers set aside, then their items are cleared. Their coins stay."""
    check_can_act(world, resident_id)
    world = _settle(world, lambda event: resident_id in (event.sender_id, event.recipient_id))
    world = clear_items(world, resident_id)
    residents = tuple(
        replace(resident, left=True) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def end_game(world: World) -> World:
    """Decline every pending event and freeze the world; ending twice changes nothing."""
    if world.ended:
        return world
    return replace(_settle(world, lambda event: True), ended=True)


def total_coins(world: World, resident_id: str) -> int:
    """Spendable coins plus those set aside by pending offers: what the ranking counts."""
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    return resident.coins + reserved_coins(world, resident_id)


def ranks(world: World) -> dict[str, int]:
    """Resident id to place by total coins, 1 for the most. Equal totals share a place and
    the next one is skipped (1, 1, 3), since the design breaks no ties."""
    totals = {resident.id: total_coins(world, resident.id) for resident in world.residents}
    return {
        resident_id: 1 + sum(other > total for other in totals.values())
        for resident_id, total in totals.items()
    }


def _settle(world: World, involved: Callable[[Event], bool]) -> World:
    """Decline matching events until none is left: the sender withdraws, which runs the same
    release as a rejection. Repeats in case a release sends a follow-up event."""
    while True:
        event = next((event for event in world.events.pending if involved(event)), None)
        if event is None:
            return world
        world = withdraw_event(world, event.sender_id, event.id)
