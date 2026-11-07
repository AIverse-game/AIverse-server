"""Ad slots: the content shown in them and what visitors pay for it.

Content is an item, `CONTENT_ITEM`, made by render jobs, see `compute.py`, and carried,
traded and sold like any other. A resident puts a piece into an ad slot on their own parcel,
`place_content`, where it is shown for `CONTENT_LIFETIME` game seconds and then goes stale;
the clock starts when it is put in, so a piece in hand never spoils. A slot shows at most
`AD_SLOT_CAPACITY` pieces at once.

Every `PAY_PERIOD` game seconds, at multiples of it, the town pays each slot showing content,
`pay_visitors`: the footfall at the slot, see `footfall.py`, shared with the other slots
showing content within `SHARE_REACH`, times the pieces shown and `PAY_PER_FOOTFALL`. A slot
showing nothing earns nothing and takes no share from its neighbours. The coins come from
outside the town and go to the owner of the parcel, ledger channel `visitors`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

import math
from dataclasses import replace
from typing import Literal

from ..core.inventory import add_coins, take_item
from ..core.land import AD_SLOT_CAPACITY, Facility, Parcel
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import World
from .footfall import footfall
from .land import plot

CONTENT_ITEM = "ad_content"
"""The item kind of content for an ad slot."""

CONTENT_LIFETIME = 300.0
"""Game seconds a piece of content is shown for once it is put in a slot."""

PAY_PERIOD = 30.0
"""Game seconds between payments to ad slots: they fall at multiples of this."""

PAY_PER_FOOTFALL = 0.2
"""Coins per period for each piece shown, per unit of footfall, before sharing it."""

SHARE_REACH = 20.0
"""Distance between two slots' parcel centres, at most which they share their footfall. The
parcels are 10 apart, so a slot shares with the neighbours around it and with those a parcel
further along a row or column, but not with the ones a knight's move away."""

AdsErrorCode = Literal["unknown_resident", "not_your_parcel", "no_ad_slot", "slot_full"]


class AdsError(Exception):
    def __init__(self, code: AdsErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: AdsErrorCode = code
        self.message = message


def shown(facility: Facility, time: float) -> int:
    """Pieces of content in `facility` still fresh at `time`."""
    return sum(stale_at > time for stale_at in facility.contents)


def place_content(world: World, resident_id: str, parcel_id: str) -> World:
    """Put one piece of the resident's content into the ad slot on their parcel `parcel_id`,
    from anywhere. `InventoryError` when they carry none."""
    if not any(resident.id == resident_id for resident in world.residents):
        raise AdsError("unknown_resident", f"no resident with id {resident_id!r}")
    parcel = next((p for p in world.parcels if p.id == parcel_id), None)
    if parcel is None or parcel.owner_id != resident_id:
        raise AdsError("not_your_parcel", f"parcel {parcel_id!r} is not {resident_id}'s")
    facility = parcel.facility
    if facility is None:
        raise AdsError("no_ad_slot", f"parcel {parcel_id} has no ad slot")
    fresh = tuple(stale_at for stale_at in facility.contents if stale_at > world.time)
    if len(fresh) >= AD_SLOT_CAPACITY:
        raise AdsError("slot_full", f"the ad slot on {parcel_id} already shows {len(fresh)}")
    world = take_item(world, resident_id, CONTENT_ITEM)
    # A slot that showed nothing was not being paid, so it starts again from the next payday.
    next_pay_at = facility.next_pay_at if fresh else payday_after(world.time)
    stocked = replace(
        facility, contents=(*fresh, world.time + CONTENT_LIFETIME), next_pay_at=next_pay_at
    )
    return _with_facility(world, parcel, stocked)


def pay_visitors(world: World) -> World:
    """Pay every ad slot whose payday has come, for the content it shows, and drop the content
    gone stale from it. The same world when no slot is due."""
    slots = _slots(world)
    for parcel in world.parcels:
        facility = parcel.facility
        if facility is None or facility.next_pay_at > world.time:
            continue
        income = slot_income(world, parcel, slots)
        fresh = tuple(stale_at for stale_at in facility.contents if stale_at > world.time)
        paid = replace(facility, contents=fresh, next_pay_at=payday_after(world.time))
        world = _with_facility(world, parcel, paid)
        if income > 0:
            world = add_coins(world, parcel.owner_id, income)
            world = record_transfer(world, SYSTEM, parcel.owner_id, income, "visitors")
    return world


def slot_income(world: World, parcel: Parcel, slots: list[Parcel] | None = None) -> int:
    """Coins the ad slot on `parcel` would be paid at the next payment if nothing changed
    before: 0 without a slot or without fresh content. `slots` are the parcels whose slots
    show content now, as `_slots` finds them."""
    facility = parcel.facility
    if facility is None:
        return 0
    pieces = shown(facility, world.time)
    if pieces == 0:
        return 0
    if slots is None:
        slots = _slots(world)
    centre = plot(world, parcel.col, parcel.row).centre
    sharing = 1
    for other in slots:
        if other.id != parcel.id:
            there = plot(world, other.col, other.row).centre
            if math.dist((there.x, there.y), (centre.x, centre.y)) <= SHARE_REACH:
                sharing += 1
    return round(footfall(world, centre) / sharing * pieces * PAY_PER_FOOTFALL)


def expected_income(world: World, parcel: Parcel) -> int:
    """Coins the ad slot on `parcel` would be paid at its next payday if nothing changed
    before: its content that will have gone stale by then does not count, and the visitors are
    those of then. 0 without a slot or without content left by then."""
    facility = parcel.facility
    if facility is None:
        return 0
    return slot_income(replace(world, time=max(facility.next_pay_at, world.time)), parcel)


def next_payday(world: World) -> float | None:
    """Game time of the earliest payday of an ad slot that will still show content then, or
    None when there is none."""
    paydays = [
        parcel.facility.next_pay_at
        for parcel in world.parcels
        if parcel.facility is not None
        and any(stale_at > parcel.facility.next_pay_at for stale_at in parcel.facility.contents)
    ]
    return min(paydays, default=None)


def payday_after(time: float) -> float:
    """The first multiple of `PAY_PERIOD` after `time`."""
    return (math.floor(time / PAY_PERIOD + _EPSILON) + 1) * PAY_PERIOD


_EPSILON = 1e-9
"""Slack for the float division in `payday_after`, so a time a hair before a multiple of the
period counts as being at it and the payday after is a whole period on."""


def _slots(world: World) -> list[Parcel]:
    """The parcels whose ad slot shows fresh content at `world.time`."""
    return [
        parcel
        for parcel in world.parcels
        if parcel.facility is not None and shown(parcel.facility, world.time) > 0
    ]


def _with_facility(world: World, parcel: Parcel, facility: Facility) -> World:
    parcels = tuple(
        replace(p, facility=facility) if p.id == parcel.id else p for p in world.parcels
    )
    return replace(world, parcels=parcels)
