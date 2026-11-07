"""Immutable land stored in the world: parcels residents own and the facilities on them; rules
live in economy/land.py and economy/facilities.py."""

import math
from dataclasses import dataclass
from typing import Literal

FacilityKind = Literal["ad", "server", "meeting"]
"""What a facility is: an ad slot, a server hosting the facilities around it, or a meeting
room, which needs a server's hosting."""


@dataclass(frozen=True)
class Facility:
    kind: FacilityKind
    built_at: float
    """Game time it was built, see `World.time`."""
    next_at: float | None
    """Game time its next cycle of visitors pays out; None for a server, which has no cycle of
    its own."""
    earned: int = 0
    """Coins it has paid its owner so far, after hosting fees; for a server, the hosting fees
    it has collected."""
    last_income: int = 0
    """Coins its latest cycle paid, after the hosting fee; 0 before the first."""
    compute: int = 0
    """Compute a server holds for hosting."""
    fee: int = 0
    """Coins a server charges a hosted facility of another owner per cycle."""
    host: str | None = None
    """Parcel id of the server that hosted a meeting room's latest cycle; None before its first
    cycle or when no server could."""

    def __post_init__(self) -> None:
        if self.kind not in ("ad", "server", "meeting"):
            raise ValueError(f"unknown facility kind {self.kind!r}")
        for amount in (self.earned, self.last_income, self.compute, self.fee):
            if type(amount) is not int or amount < 0:
                raise ValueError(f"facility amount {amount!r}, must be an integer >= 0")
        if not math.isfinite(self.built_at) or self.built_at < 0:
            raise ValueError(f"facility built at {self.built_at!r}")
        if (self.next_at is None) != (self.kind == "server"):
            raise ValueError("every facility but a server pays out in cycles")
        if self.next_at is not None and not (
            math.isfinite(self.next_at) and self.built_at < self.next_at
        ):
            raise ValueError("a facility pays out after it is built")
        if self.kind != "server" and (self.compute or self.fee):
            raise ValueError("only a server holds compute and charges a fee")
        if self.kind != "meeting" and self.host is not None:
            raise ValueError("only a meeting room is hosted")


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
