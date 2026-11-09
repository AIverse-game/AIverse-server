"""Cues: moments a page may act out on a resident, derived from what was done.

A resident talks when they say something or put a request to someone, and is happy when
something they wanted works out: their request is accepted, or a facility they built is
finished. Cues are only for show, so they are not part of the world.
"""

from typing import Literal

from ..core.world import World
from .actions import (
    Action,
    OfferBuildCommission,
    OfferInformation,
    OfferJointPurchase,
    OfferShares,
    OfferToBuy,
    OfferToSell,
    ProposeContract,
    RespondToEvent,
    Say,
)

CueKind = Literal["talk", "happy"]

_TALKING = (
    Say
    | OfferToBuy
    | OfferToSell
    | OfferInformation
    | OfferShares
    | OfferJointPurchase
    | OfferBuildCommission
    | ProposeContract
)


def cues(before: World, action: Action) -> list[tuple[str, CueKind]]:
    """The (resident id, "talk" or "happy") cues of an action that was carried out on `before`:
    the sender and the recipient are both happy when an answer accepts a request."""
    if isinstance(action, _TALKING):
        return [(action.resident_id, "talk")]
    if isinstance(action, RespondToEvent):
        if not action.accept:
            return [(action.resident_id, "talk")]
        sender = next(
            (event.sender_id for event in before.events.pending if event.id == action.event_id),
            None,
        )
        happy: list[tuple[str, CueKind]] = [(action.resident_id, "happy")]
        if sender is not None:
            happy.append((sender, "happy"))
        return happy
    return []


def new_facility_owners(before: World, after: World) -> list[str]:
    """Who owns each parcel that has a finished facility in `after` and had none in `before`."""
    built = {parcel.id for parcel in before.parcels if parcel.facility is not None}
    return [
        parcel.owner_id
        for parcel in after.parcels
        if parcel.facility is not None and parcel.id not in built
    ]
