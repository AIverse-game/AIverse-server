"""A game's lifecycle: who may still act, residents leaving, the game ending, the ranking.

A game runs for a fixed time after the server creates its world, `GAME_DURATION` unless the
server is started with another; the clock itself lives with the server, see `app.py`. When
it runs out, `end_game` settles every pending event and ends every contract, returning what
offers and contracts set aside, and from then on nothing changes. Residents may leave
earlier, of their own accord or because the server makes them; the game goes on without
them. They stay in the world and in the ranking with the coins they had, but lose their
items and can no longer act or trade. Running out of tokens, the residents' stamina, takes
them out the same way, see `knock_out_exhausted` and `knock_out_arrivals`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from ..core.events import Event, withdraw_event
from ..core.inventory import clear_items
from ..core.world import World
from ..economy.contracts import contract_coins, end_contracts
from ..economy.mining import cancel_mining
from ..economy.tokens import token_total
from ..economy.trade import reserved_coins
from ..residents.walking import stop_walking

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
    """Take the resident out of the game: a mining job stops and its tokens go back to the
    pool, a walk stops where they are, their pending events are declined and their contracts
    end, which hands back whatever offers and contracts set aside, a supplier's deposit
    going to the buyer, then their items are cleared. Their coins stay."""
    check_can_act(world, resident_id)
    world = cancel_mining(world, resident_id)
    world = stop_walking(world, resident_id)
    world = _settle(world, lambda event: resident_id in (event.sender_id, event.recipient_id))
    world = end_contracts(
        world,
        lambda contract: resident_id in (contract.supplier_id, contract.buyer_id),
        "party_left",
        resident_id,
    )
    world = clear_items(world, resident_id)
    residents = tuple(
        replace(resident, left=True) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def knock_out_exhausted(before: World, after: World) -> World:
    """Take out of the game, as `leave_game` does, every resident still in it who owned
    tokens in `before` and owns none in `after`. Tokens set aside in sale offers count, and
    so do those a running mining job has already dug out, since both come back to them.
    Comparing the two worlds catches whatever spent the last token: a walk or a sale.
    Residents still walking are left alone: they reach where they paid to go, and
    `knock_out_arrivals` takes them out there."""
    already_out = {
        resident.id for resident in after.residents if resident.left or resident.walking is not None
    }
    for resident in before.residents:
        if resident.id in already_out or _owned_tokens(before, resident.id) == 0:
            continue
        if _owned_tokens(after, resident.id) == 0:
            after = leave_game(after, resident.id)
    return after


def knock_out_arrivals(before: World, after: World) -> World:
    """Take out of the game, as `leave_game` does, every resident who was walking in `before`,
    has arrived in `after` and owns no tokens: the walk or a sale on the way spent the last."""
    for resident in before.residents:
        if resident.walking is None:
            continue
        now = next(other for other in after.residents if other.id == resident.id)
        if not now.left and now.walking is None and _owned_tokens(after, resident.id) == 0:
            after = leave_game(after, resident.id)
    return after


def _owned_tokens(world: World, resident_id: str) -> int:
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    mined = resident.mining.amount if resident.mining is not None else 0
    return token_total(world, resident_id) + mined


def end_game(world: World) -> World:
    """Stop every mining job and walk, decline every pending event, end every contract and
    freeze the world; ending twice changes nothing."""
    if world.ended:
        return world
    for resident in world.residents:
        world = cancel_mining(world, resident.id)
        world = stop_walking(world, resident.id)
    world = end_contracts(_settle(world, lambda event: True), lambda contract: True, "game_ended")
    return replace(world, ended=True)


def total_coins(world: World, resident_id: str) -> int:
    """Spendable coins plus those set aside by pending offers and contracts: what the ranking
    counts."""
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    return resident.coins + reserved_coins(world, resident_id) + contract_coins(world, resident_id)


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
