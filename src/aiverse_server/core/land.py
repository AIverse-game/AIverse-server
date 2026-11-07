"""Immutable land stored in the world: parcels residents own; rules live in economy/land.py."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Parcel:
    """A square of land a resident bought, `col` and `row` counted from the north-west
    corner; parcels nobody owns are not stored."""

    col: int
    row: int
    owner_id: str

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
