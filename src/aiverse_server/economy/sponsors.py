"""Sponsors: outside buyers who pay for the views the scripted visitors give ad slots.

A sponsor is the system playing a party from outside the town, like the vendors of compute.
Each has a budget that fills at `Sponsor.rate` coins for each game second, up to
`BUDGET_CAP_SECONDS` worth, and pays `Sponsor.price` coins for each person who saw an ad slot,
see `visitors.py`. Views are paid for at paydays, which fall at multiples of `PAY_PERIOD`, all
slots at once, `pay_sponsors`. The payday of a multiple is held at the first moment on or after
it when there are views to pay for, which is the multiple itself when the clock is on time, a
few milliseconds later when it is not, and the first step of a visitor after it when nobody had
seen a slot before; a multiple is held at most once, however late, and it counts as the time the
budgets were topped up to: the views each slot has had since the last one are pooled and
the sponsors who pay most buy them first, as many as their budgets allow. When the budgets
fall short, every slot is paid for the same share of its views, at the average price paid, and
the views left over are not paid for at all and are not carried over. The coins come from
outside the town and go to the holders of the parcel's shares in proportion, see `shares.py`,
ledger channel `sponsors`.

Slots are paid by views alone, so more content helps a slot only by drawing more visitors. A
slot showing nothing gets no visitors and so no pay; views it had before its content went
stale are still paid at the next payday.

Like the rest of the world, every function returns a new world and leaves its argument
untouched.
"""

import math
from dataclasses import replace

from ..core.inventory import add_coins
from ..core.ledger import SYSTEM, record_transfer
from ..core.sponsors import Sponsor, SponsorBook
from ..core.world import World
from .shares import split_income

PAY_PERIOD = 30.0
"""Game seconds between paydays: they fall at multiples of this."""

BUDGET_CAP_SECONDS = 120.0
"""A sponsor's budget stops filling at this many seconds of its `rate`, so that a quiet spell
does not leave a pile to spend all at once."""

_EPSILON = 1e-9
"""Slack for the float division by `PAY_PERIOD`, so a time a hair off a multiple of it counts
as being at it."""


def default_sponsors() -> SponsorBook:
    """The three sponsors of a game, with empty budgets that fill from the start. Together they
    put in 10 coins a second, and can pay for about 6 views a second at the prices they bid."""
    return SponsorBook(
        sponsors=(
            Sponsor(0, "星河饮品", price=2.4, rate=3.0),
            Sponsor(1, "云端出行", price=1.8, rate=4.0),
            Sponsor(2, "拾光书店", price=1.2, rate=3.0),
        )
    )


def unsettled_views(world: World) -> dict[str, float]:
    """People who have seen each slot since it was last dealt with, by parcel; slots nobody has
    seen since are left out."""
    done = dict(world.sponsors.settled)
    waiting = {}
    for parcel_id, people in world.crowd.views:
        left = people - done.get(parcel_id, 0.0)
        if left > _EPSILON:
            waiting[parcel_id] = left
    return waiting


def pay_sponsors(world: World) -> World:
    """At a payday, pay every slot for the views it has had since the last one, as the module
    docstring describes, and top up the sponsors' budgets. The same world when no multiple of
    the period has come since the last payday, when there are no sponsors or when nobody has seen
    a slot since."""
    book = world.sponsors
    payday = latest_payday(world.time)
    if not book.sponsors or payday <= book.topped_up_at:
        return world
    waiting = unsettled_views(world)
    if not waiting:
        return world
    elapsed = payday - book.topped_up_at
    funds = {s.id: min(s.funds + s.rate * elapsed, budget_cap(s)) for s in book.sponsors}
    people = sum(waiting.values())
    to_buy = people
    spent = 0.0
    for sponsor in sorted(book.sponsors, key=lambda s: (-s.price, s.id)):
        bought = min(funds[sponsor.id] / sponsor.price, to_buy)
        # The product can come out a hair over the budget it was divided from.
        cost = min(bought * sponsor.price, funds[sponsor.id])
        funds[sponsor.id] -= cost
        spent += cost
        to_buy -= bought
    average = spent / people
    paid: dict[str, int] = {}
    for parcel in world.parcels:
        if parcel.id not in waiting:
            continue
        income = round(waiting[parcel.id] * average)
        if income:
            paid[parcel.id] = income
            for holder, part in split_income(parcel, income):
                world = add_coins(world, holder, part)
                world = record_transfer(world, SYSTEM, holder, part, "sponsors")
    views = dict(world.crowd.views)
    settled = {**dict(book.settled), **{p: views[p] for p in waiting}}
    sponsors = tuple(replace(s, funds=funds[s.id]) for s in book.sponsors)
    return replace(
        world,
        sponsors=SponsorBook(
            sponsors=sponsors,
            topped_up_at=payday,
            settled=tuple(sorted(settled.items())),
            last_paid=tuple(sorted(paid.items())),
        ),
    )


def next_payday(world: World) -> float | None:
    """Game time when the next payday that has views to pay for is held: now when a multiple of
    the period has passed since the last one, else the next multiple. None when there is
    nothing to pay for or no sponsor to pay."""
    if not world.sponsors.sponsors or not unsettled_views(world):
        return None
    return next_payday_after(world.time, world.sponsors.topped_up_at)


def next_payday_after(time: float, last: float) -> float:
    """When a payday is held, given that the last one was the multiple `last` of the period and
    it is `time` now: at once when a multiple has passed since, else at the next multiple."""
    if latest_payday(time) > last:
        return time
    return latest_payday(time) + PAY_PERIOD


def budget_cap(sponsor: Sponsor) -> float:
    """The most budget the sponsor holds."""
    return sponsor.rate * BUDGET_CAP_SECONDS


def latest_payday(time: float) -> float:
    """The last multiple of `PAY_PERIOD` that is not after `time`."""
    return math.floor(time / PAY_PERIOD + _EPSILON) * PAY_PERIOD
