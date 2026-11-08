"""Ad slots and game halls: the content shown in them and what visitors pay for it.

Content is an item made by render jobs, see `compute.py`, and carried, traded and sold like
any other. Each facility that shows content takes its own kind: `CONTENT_ITEM` for ad slots,
`GAME_ITEM` for game halls, see `ITEM_FOR`. A resident puts a piece into the matching
facility on their own parcel, `place_content`, where it is shown for `CONTENT_LIFETIME` game
seconds and then goes stale; the clock starts when it is put in, so a piece in hand never
spoils. A facility shows at most `AD_SLOT_CAPACITY` pieces at once.

A game hall shows games and a workspace shows nothing, see `compute.py` for how they are built;
both earn from the fees visitors pay at the door, see `admissions.py`, and a game hall draws
them only while it shows a game, see `visitors.py`. An ad slot earns from the people who pass it
while it shows content, see `sponsors.py`; nobody goes to look at one.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import take_item
from ..core.land import AD_SLOT_CAPACITY, Facility, FacilityKind, Parcel
from ..core.world import World

CONTENT_ITEM = "ad_content"
"""The item kind of content for an ad slot."""

GAME_ITEM = "game_content"
"""The item kind of a game, the content for a game hall."""

ITEM_FOR: dict[FacilityKind, str] = {"ad": CONTENT_ITEM, "game_hall": GAME_ITEM}
"""The item kind each facility that shows content takes."""

CONTENT_LIFETIME = 300.0
"""Game seconds a piece of content is shown for once it is put in a slot."""

AdsErrorCode = Literal["unknown_resident", "not_your_parcel", "no_content_facility", "slot_full"]


class AdsError(Exception):
    def __init__(self, code: AdsErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: AdsErrorCode = code
        self.message = message


def shown(facility: Facility, time: float) -> int:
    """Pieces of content in `facility` still fresh at `time`."""
    return sum(stale_at > time for stale_at in facility.contents)


def place_content(world: World, resident_id: str, parcel_id: str) -> World:
    """Put one piece of the resident's content into the ad slot or game hall on their parcel
    `parcel_id`, from anywhere: the kind of content the facility takes, see `ITEM_FOR`.
    `InventoryError` when they carry none."""
    if not any(resident.id == resident_id for resident in world.residents):
        raise AdsError("unknown_resident", f"no resident with id {resident_id!r}")
    parcel = next((p for p in world.parcels if p.id == parcel_id), None)
    if parcel is None or parcel.owner_id != resident_id:
        raise AdsError("not_your_parcel", f"parcel {parcel_id!r} is not {resident_id}'s")
    facility = parcel.facility
    if facility is None or facility.kind not in ITEM_FOR:
        raise AdsError(
            "no_content_facility", f"parcel {parcel_id} has no ad slot or game hall to show it"
        )
    fresh = tuple(stale_at for stale_at in facility.contents if stale_at > world.time)
    if len(fresh) >= AD_SLOT_CAPACITY:
        raise AdsError(
            "slot_full", f"the {facility.kind} on {parcel_id} already shows {len(fresh)}"
        )
    world = take_item(world, resident_id, ITEM_FOR[facility.kind])
    stocked = replace(facility, contents=(*fresh, world.time + CONTENT_LIFETIME))
    return _with_facility(world, parcel, stocked)


def _with_facility(world: World, parcel: Parcel, facility: Facility) -> World:
    parcels = tuple(
        replace(p, facility=facility) if p.id == parcel.id else p for p in world.parcels
    )
    return replace(world, parcels=parcels)
