"""Immutable mining equipment stored in the world; rules live in economy/rigs.py."""

import math
from dataclasses import dataclass

RIG_CAPACITY = 4


@dataclass(frozen=True)
class RigSlot:
    id: int
    resident_id: str
    chip: str
    balance: int
    next_at: float
    mined: int = 0
    rent_paid: int = 0

    def __post_init__(self) -> None:
        for amount, minimum in (
            (self.id, 1),
            (self.balance, 0),
            (self.mined, 0),
            (self.rent_paid, 0),
        ):
            if type(amount) is not int or amount < minimum:
                raise ValueError("invalid rig slot amount")
        if (
            not self.resident_id
            or self.chip not in ("MK1", "MK2", "MK3")
            or not math.isfinite(self.next_at)
            or self.next_at < 0
        ):
            raise ValueError("invalid rig slot identity or time")


@dataclass(frozen=True)
class MiningRig:
    id: int
    owner_id: str
    pool_id: str
    slots: tuple[RigSlot, ...] = ()
    next_slot_id: int = 1

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id < 1 or not self.owner_id or not self.pool_id:
            raise ValueError("invalid mining rig identity")
        if type(self.next_slot_id) is not int or self.next_slot_id < 1:
            raise ValueError("invalid next slot id")
        ids = [slot.id for slot in self.slots]
        if len(ids) > RIG_CAPACITY:
            raise ValueError("too many rig slots")
        if len(set(ids)) != len(ids) or any(not 1 <= id < self.next_slot_id for id in ids):
            raise ValueError("invalid mining rig slot ids")
