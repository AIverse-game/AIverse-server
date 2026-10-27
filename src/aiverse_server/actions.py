"""Operation primitives: the only ways players and AI change the world.

A primitive is a small immutable request, such as "move this resident to (x, y)". Players
send them by hand now; AI-driven residents will send the same ones later. `perform` checks
a request against the current world and either returns the next world or raises
`ActionRejected`, leaving the world untouched. Requests come from untrusted sources, so
every rule lives here rather than with the caller.
"""

from dataclasses import dataclass, replace
from typing import Literal

from .world import Position, World

RejectionCode = Literal["unknown_resident", "out_of_bounds"]


class ActionRejected(Exception):
    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code: RejectionCode = code
        self.message = message


@dataclass(frozen=True)
class MoveTo:
    """Put a resident at `target`.

    The move is instant and in a straight line: speed, per-round distance and obstacles
    are still open design questions, see docs/status.md in the client repository.
    """

    resident_id: str
    target: Position


# The only primitive so far; becomes a union, dispatched in `perform`, as more arrive.
Action = MoveTo


def perform(world: World, action: Action) -> World:
    return _move_to(world, action)


def _move_to(world: World, action: MoveTo) -> World:
    if all(resident.id != action.resident_id for resident in world.residents):
        raise ActionRejected("unknown_resident", f"no resident with id {action.resident_id!r}")
    target = action.target
    if not world.map.contains(target):
        size = world.map.size
        raise ActionRejected(
            "out_of_bounds",
            f"target ({target.x:g}, {target.y:g}) is outside the map [0, {size:g}]",
        )
    residents = tuple(
        replace(resident, position=target) if resident.id == action.resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
