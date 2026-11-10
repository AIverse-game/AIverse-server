"""Admission fees: what visitors pay at the door of a workspace, a game hall or a mall.

The system sets one fee for each kind of facility, `FEES`, in coins for one person. A visitor
stands for more people as the game goes on, `visitors.visitor_weight`, and pays for all of
them: the fee times that. The money comes from outside the town, and is bounded by how many
visitors there are and how often they go in. A facility is not paid at the door but, like the
ad slots, at the paydays of `sponsors.py`: the fees taken since the last payday are paid out to
the holders of the parcel's shares, see `shares.py`, in whole coins, the part of a coin left
carried to the next payday. Ledger channel `visitors`. A garden in the block adds a tenth,
`gardens.py`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched.
"""

from dataclasses import replace

from ..core.admissions import AdmissionBook
from ..core.inventory import add_coins
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import World
from .gardens import income_multiplier, pay_bonus
from .shares import split_income
from .sponsors import latest_payday, next_payday_after

FEES: dict[str, int] = {"workspace": 8, "game_hall": 12, "mall": 2, "garden": 1}
"""Coins one person pays to get into a facility of each kind."""

_SLACK = 1e-9


def due_fees(world: World) -> dict[str, int]:
    """Whole coins of fees waiting to be paid out, by parcel, the tenth a garden in the block adds
    included; parcels owed less than a coin are left out."""
    due = {
        parcel_id: int(amount * income_multiplier(world, parcel_id) + _SLACK)
        for parcel_id, amount in world.admissions.unpaid
    }
    return {parcel_id: coins for parcel_id, coins in due.items() if coins >= 1}


def pay_admissions(world: World) -> World:
    """At a payday, pay out the whole coins of fees taken since the last one to the holders of
    each facility's parcel. The same world when no payday has come since the last one or when no
    facility is owed a whole coin."""
    book = world.admissions
    payday = latest_payday(world.time)
    if payday <= book.paid_at:
        return world
    owed = due_fees(world)
    if not owed:
        return world
    unpaid = dict(book.unpaid)
    for parcel in world.parcels:
        coins = owed.get(parcel.id, 0)
        if not coins:
            continue
        # What is paid is the fees taken times the multiplier; the fees it settles are the
        # coins divided by it, and what is left carries over. The fees' own share is what they
        # come to in whole coins, the garden's tenth the rest.
        taken = unpaid[parcel.id]
        left = taken - coins / income_multiplier(world, parcel.id)
        fees = min(int(taken + _SLACK), coins)
        if left > _SLACK:
            unpaid[parcel.id] = left
        else:
            del unpaid[parcel.id]
        for holder, part in split_income(parcel, fees):
            world = add_coins(world, holder, part)
            world = record_transfer(world, SYSTEM, holder, part, "visitors")
        world = pay_bonus(world, parcel.id, coins - fees)
    return replace(
        world,
        admissions=AdmissionBook(
            unpaid=tuple(sorted(unpaid.items())),
            admitted=book.admitted,
            paid_at=payday,
            last_paid=tuple(sorted(owed.items())),
        ),
    )


def next_admission_payday(world: World) -> float | None:
    """Game time when fees waiting to be paid out are, or None when none is owed a whole coin."""
    if not due_fees(world):
        return None
    return next_payday_after(world.time, world.admissions.paid_at)


def admit(book: AdmissionBook, parcel_id: str, people: float, fee: int) -> AdmissionBook:
    """`book` after `people` went into the facility on `parcel_id` at `fee` coins each."""
    unpaid = dict(book.unpaid)
    unpaid[parcel_id] = unpaid.get(parcel_id, 0.0) + people * fee
    admitted = dict(book.admitted)
    admitted[parcel_id] = admitted.get(parcel_id, 0.0) + people
    return replace(
        book, unpaid=tuple(sorted(unpaid.items())), admitted=tuple(sorted(admitted.items()))
    )
