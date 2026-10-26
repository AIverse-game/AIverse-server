"""Authoritative world state: a finite square map with a continuous coordinate system.

Coordinates: the origin (0, 0) is the north-west (top-left) corner, x grows east
(right) and y grows south (down), matching screen space. Both axes cover the closed
range [0, size]. Positions are real numbers, not grid cells: residents, and later
places and items, can stand anywhere inside the map.
"""

import math
import re
from dataclasses import dataclass

COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")


@dataclass(frozen=True)
class Position:
    x: float
    y: float


@dataclass(frozen=True)
class Resident:
    id: str
    name: str
    color: str
    """Display colour as `#rrggbb`."""
    position: Position

    def __post_init__(self) -> None:
        if not COLOR_PATTERN.fullmatch(self.color):
            raise ValueError(f"resident {self.id} has an invalid color: {self.color!r}")


@dataclass(frozen=True)
class WorldMap:
    """Side length of the square map, in map units."""

    size: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.size) and self.size > 0):
            raise ValueError(f"map size must be a positive finite number, got {self.size}")

    def contains(self, position: Position) -> bool:
        # NaN fails every comparison, so it is never inside the map.
        return 0 <= position.x <= self.size and 0 <= position.y <= self.size


@dataclass(frozen=True)
class World:
    map: WorldMap
    residents: tuple[Resident, ...]

    def __post_init__(self) -> None:
        ids = [resident.id for resident in self.residents]
        if len(set(ids)) != len(ids):
            raise ValueError("resident ids must be unique")
        for resident in self.residents:
            if not self.map.contains(resident.position):
                raise ValueError(f"resident {resident.id} is outside the map: {resident.position}")
