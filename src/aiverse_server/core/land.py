"""Immutable land stored in the world: parcels residents own and the facilities on them; rules
live in economy/land.py and economy/compute.py."""

import math
from dataclasses import dataclass
from typing import Literal

FacilityKind = Literal["ad"]
"""What a facility is. Only the ad slot exists so far; other kinds come with the designs that
need them, see the client repository's docs/design/facilities.md."""


AD_SLOT_CAPACITY = 5
"""Pieces of content an ad slot shows at once."""


@dataclass(frozen=True)
class Facility:
    kind: FacilityKind
    built_at: float
    """Game time it was finished, see `World.time`."""
    contents: tuple[float, ...] = ()
    """The content shown in it, as the game times when each piece goes stale, soonest first;
    pieces that have gone stale may linger until the facility is next paid, see
    `economy/ads.py`."""
    next_pay_at: float = 0.0
    """Game time the town next pays it for the content it shows, see `economy/ads.py`."""

    def __post_init__(self) -> None:
        if self.kind != "ad":
            raise ValueError(f"unknown facility kind {self.kind!r}")
        if not math.isfinite(self.built_at) or self.built_at < 0:
            raise ValueError(f"facility built at {self.built_at!r}")
        if len(self.contents) > AD_SLOT_CAPACITY:
            raise ValueError(
                f"facility holds {len(self.contents)} pieces of content, over the limit"
            )
        if not (math.isfinite(self.next_pay_at) and self.next_pay_at >= 0):
            raise ValueError(f"facility is paid at {self.next_pay_at!r}")
        if not all(math.isfinite(time) for time in self.contents):
            raise ValueError("content goes stale at a time that is not finite")
        object.__setattr__(self, "contents", tuple(sorted(self.contents)))


@dataclass(frozen=True)
class Parcel:
    """A square of land a resident bought, `col` and `row` counted from the north-west
    corner; parcels nobody owns are not stored."""

    col: int
    row: int
    owner_id: str
    facility: Facility | None = None

    @property
    def id(self) -> str:
        return parcel_id(self.col, self.row)

    def __post_init__(self) -> None:
        for index in (self.col, self.row):
            if type(index) is not int or index < 0:
                raise ValueError(f"parcel index {index!r}, must be an integer >= 0")
        if not self.owner_id:
            raise ValueError("a parcel needs an owner")


def parcel_id(col: int, row: int) -> str:
    """How residents name a parcel, such as `L7-3` for column 7, row 3."""
    return f"L{col}-{row}"
