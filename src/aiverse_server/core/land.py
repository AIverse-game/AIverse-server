"""Immutable land stored in the world: parcels residents own and the facilities on them; rules
live in economy/land.py and economy/compute.py."""

import math
from dataclasses import dataclass
from typing import Literal

FacilityKind = Literal["ad"]
"""What a facility is. Only the ad slot exists so far; other kinds come with the designs that
need them, see the client repository's docs/design/facilities.md."""


@dataclass(frozen=True)
class Facility:
    kind: FacilityKind
    built_at: float
    """Game time it was finished, see `World.time`."""

    def __post_init__(self) -> None:
        if self.kind != "ad":
            raise ValueError(f"unknown facility kind {self.kind!r}")
        if not math.isfinite(self.built_at) or self.built_at < 0:
            raise ValueError(f"facility built at {self.built_at!r}")


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
