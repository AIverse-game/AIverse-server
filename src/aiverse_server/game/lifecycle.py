"""A game's lifecycle: who may still act, residents leaving, the game ending, the ranking.

A game runs for a fixed time after the server creates its world, `GAME_DURATION` unless the
server is started with another; the clock itself lives with the server, see `app.py`. When
it runs out, `end_game` settles every pending event and ends every contract, returning what
offers and contracts set aside, and from then on nothing changes. Residents may leave
earlier, of their own accord or because the server makes them; the game goes on without
them. They stay in the world and in the ranking with the coins they had, but lose their
items and can no longer act or trade. Running out of energy, the residents' stamina, takes
them out the same way, see `knock_out_exhausted` and `knock_out_arrivals`.

The ranking counts each resident's score: their coins, see `total_coins`, plus what their
energy and crystals are worth, see `item_value`. It is live during the game and final once
it ends, when the market price stops moving.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from ..core.events import Event, withdraw_event
from ..core.inventory import clear_items, item_count
from ..core.orders import order_coins, order_items
from ..core.world import Notice, NoticeLog, World
from ..economy.contracts import contract_coins, end_contracts
from ..economy.crystals import CRYSTAL_TIERS
from ..economy.energy import energy_total
from ..economy.exchange import cancel_orders
from ..economy.harvesting import cancel_harvesting
from ..economy.market import unit_price
from ..economy.shards import cancel_digging
from ..economy.towers import release_towers, tower_coins
from ..economy.trade import reserved_coins, reserved_items
from ..economy.workshop import cancel_refining
from ..residents.walking import stop_walking

GAME_DURATION = 20 * 60.0
"""Seconds from the world's creation to the end of the game."""

ENDING_NOTICE_SECONDS = 60.0
"""How long before the game ends the server tells every resident it is about to."""

EARLY_ENDING_NOTICE_SECONDS = 180.0
"""Earlier warning so a slow model can still act before the final minute."""

CRYSTAL_SCORE_SHARE = 0.5
"""Share of a crystal's import price it counts for in the ranking."""

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
    """Take the resident out of the game: a harvesting or digging job stops and what it took
    goes back to the spring or quarry, a refining job is dropped, a walk stops where they are,
    their orders are withdrawn, their pending events are declined and their contracts end,
    which hands back whatever orders, offers and contracts set aside, a supplier's deposit
    going to the buyer, then their items are cleared. Their coins stay."""
    check_can_act(world, resident_id)
    world = cancel_harvesting(world, resident_id)
    world = cancel_digging(world, resident_id)
    world = cancel_refining(world, resident_id)
    world = release_towers(world, resident_id)
    world = stop_walking(world, resident_id)
    world = cancel_orders(world, resident_id)
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
    energy in `before` and owns none in `after`. Energy set aside in sale offers and sell
    orders counts, and so does what a running harvesting job has already drawn, since all of
    it comes back to them. Comparing the two worlds catches whatever spent the last energy: a
    walk, a sale, or someone buying a sell order out.
    Residents still walking are left alone: they reach where they paid to go, and
    `knock_out_arrivals` takes them out there."""
    already_out = {
        resident.id for resident in after.residents if resident.left or resident.walking is not None
    }
    for resident in before.residents:
        if resident.id in already_out or _owned_energy(before, resident.id) == 0:
            continue
        if _owned_energy(after, resident.id) == 0:
            after = leave_game(after, resident.id)
    return after


def knock_out_arrivals(before: World, after: World) -> World:
    """Take out of the game, as `leave_game` does, every resident who was walking in `before`,
    has arrived in `after` and owns no energy: the walk or a sale on the way spent the last."""
    for resident in before.residents:
        if resident.walking is None:
            continue
        now = next(other for other in after.residents if other.id == resident.id)
        if not now.left and now.walking is None and _owned_energy(after, resident.id) == 0:
            after = leave_game(after, resident.id)
    return after


def _owned_energy(world: World, resident_id: str) -> int:
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    harvested = resident.harvesting.amount if resident.harvesting is not None else 0
    return energy_total(world, resident_id) + harvested


def announce(world: World, text: str) -> World:
    """Tell every resident `text`; each reads it in their next look, see
    `perception.begin_turn`."""
    log = world.notices
    notice = Notice(log.next_seq, world.time, text)
    return replace(world, notices=NoticeLog((*log.entries, notice), log.next_seq + 1))


def end_game(world: World) -> World:
    """Stop every job and walk, putting back what harvesting and digging took, decline every
    pending event, withdraw every order, end every contract and freeze the world; ending twice
    changes nothing."""
    if world.ended:
        return world
    for resident in world.residents:
        world = cancel_harvesting(world, resident.id)
        world = cancel_digging(world, resident.id)
        world = cancel_refining(world, resident.id)
        world = stop_walking(world, resident.id)
    world = end_contracts(_settle(world, lambda event: True), lambda contract: True, "game_ended")
    world = release_towers(cancel_orders(world))
    return replace(world, ended=True)


def total_coins(world: World, resident_id: str) -> int:
    """Spendable coins plus those set aside by pending offers, contracts, towers and buy
    orders; the ranking adds the worth of items, see `score`."""
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    return (
        resident.coins
        + reserved_coins(world, resident_id)
        + contract_coins(world, resident_id)
        + tower_coins(world, resident_id)
        + order_coins(world.orders, resident_id)
    )


def item_value(world: World, resident_id: str) -> int:
    """Coins the resident's energy and crystals count for in the ranking, rounded down:
    energy at the market's unit price right now, and crystals at `CRYSTAL_SCORE_SHARE` of
    their import price. Both count what the resident carries and what offers and sell orders
    set aside; crystals installed in towers count too, since they come back. Shards count
    nothing, as nobody outside the town buys them, and neither does what running jobs will
    pay, since the end of the game voids them."""
    energy = energy_total(world, resident_id)
    price = unit_price(world.market, world.time) if world.market is not None else 0.0
    installed = [
        slot.crystal
        for tower in world.towers
        for slot in tower.slots
        if slot.resident_id == resident_id
    ]
    crystals = sum(
        tier.price
        * (
            item_count(world, resident_id, tier.tier)
            + reserved_items(world, resident_id, tier.tier)
            + order_items(world.orders, resident_id, tier.tier)
            + installed.count(tier.tier)
        )
        for tier in CRYSTAL_TIERS
    )
    return math.floor(energy * price + CRYSTAL_SCORE_SHARE * crystals)


def score(world: World, resident_id: str) -> int:
    """What the ranking counts: total coins plus the worth of energy and crystals."""
    return total_coins(world, resident_id) + item_value(world, resident_id)


def ranks(world: World) -> dict[str, int]:
    """Resident id to place by score, 1 for the most. Equal scores share a place and the
    next one is skipped (1, 1, 3), since the design breaks no ties."""
    totals = {resident.id: score(world, resident.id) for resident in world.residents}
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
