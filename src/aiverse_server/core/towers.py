"""Immutable harvesting equipment stored in the world; rules live in economy/towers.py."""

import math
from dataclasses import dataclass

TOWER_CAPACITY = 4


@dataclass(frozen=True)
class TowerSlot:
    id: int
    resident_id: str
    crystal: str
    balance: int
    next_at: float
    harvested: int = 0
    rent_paid: int = 0

    def __post_init__(self) -> None:
        for amount, minimum in (
            (self.id, 1),
            (self.balance, 0),
            (self.harvested, 0),
            (self.rent_paid, 0),
        ):
            if type(amount) is not int or amount < minimum:
                raise ValueError("invalid tower slot amount")
        if (
            not self.resident_id
            or self.crystal not in ("C1", "C2", "C3")
            or not math.isfinite(self.next_at)
            or self.next_at < 0
        ):
            raise ValueError("invalid tower slot identity or time")


@dataclass(frozen=True)
class Tower:
    id: int
    owner_id: str
    spring_id: str
    slots: tuple[TowerSlot, ...] = ()
    next_slot_id: int = 1

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id < 1 or not self.owner_id or not self.spring_id:
            raise ValueError("invalid tower identity")
        if type(self.next_slot_id) is not int or self.next_slot_id < 1:
            raise ValueError("invalid next slot id")
        ids = [slot.id for slot in self.slots]
        if len(ids) > TOWER_CAPACITY:
            raise ValueError("too many tower slots")
        if len(set(ids)) != len(ids) or any(not 1 <= id < self.next_slot_id for id in ids):
            raise ValueError("invalid tower slot ids")
