"""Immutable land stored in the world: parcels residents own and the facilities on them; rules
live in economy/land.py and economy/facilities.py."""

import math
from dataclasses import dataclass
from typing import Literal

FacilityKind = Literal["ad"]
"""What a facility is: so far only an ad slot."""


@dataclass(frozen=True)
class Facility:
    kind: FacilityKind
    built_at: float
    """Game time it was built, see `World.time`."""
    next_at: float
    """Game time its next cycle of visitors pays out."""
    earned: int = 0
    """Coins it has paid its owner so far."""
    last_income: int = 0
    """Coins its latest cycle paid; 0 before the first."""

    def __post_init__(self) -> None:
        if self.kind != "ad":
            raise ValueError(f"unknown facility kind {self.kind!r}")
        for amount in (self.earned, self.last_income):
            if type(amount) is not int or amount < 0:
                raise ValueError(f"facility income {amount!r}, must be an integer >= 0")
        if not (
            math.isfinite(self.built_at)
            and math.isfinite(self.next_at)
            and 0 <= self.built_at < self.next_at
        ):
            raise ValueError("a facility pays out after it is built")


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
