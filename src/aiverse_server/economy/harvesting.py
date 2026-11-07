"""Harvesting: residents spend time at a spring to earn compute.

A resident standing within a spring's radius starts a job that lasts `HARVEST_SECONDS` of game
time. They need a program and use the best one they carry, see `programs.py`. The compute,
as much as that tier draws per job or whatever the spring has left if less, comes out of the
spring at once, so two harvesters cannot draw the same compute, and reaches the harvester when
the job ends. Until then the harvester is busy and stays with the program: they can do nothing
but talk, or leave the game, which puts the compute back into the spring. Walkers are busy the
same way, see `walking.py`, and so are gatherers and writers, see `gathering.py` and `studio.py`;
`check_not_busy` covers them all.

Game time is `World.time`. The server moves it on as real time passes and calls
`finish_harvesting` to pay out the jobs that are due; `next_finish` tells it when to look again.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import store_item
from ..core.world import HarvestJob, Resident, Spring, World
from .compute import COMPUTE
from .programs import best_program

HARVEST_SECONDS = 10.0
"""Game seconds one harvesting job takes."""

HarvestErrorCode = Literal[
    "unknown_resident", "unknown_spring", "not_at_spring", "spring_empty", "busy", "no_program"
]


class HarvestError(Exception):
    def __init__(self, code: HarvestErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: HarvestErrorCode = code
        self.message = message


def check_not_busy(world: World, resident_id: str) -> None:
    """Refuse a resident who is in the middle of a walk or a job: harvesting, gathering data or
    writing a program."""
    resident = _find(world, resident_id)
    doing = (
        "harvesting"
        if resident.harvesting is not None
        else "walking"
        if resident.walking is not None
        else "gathering"
        if resident.gathering is not None
        else "writing"
        if resident.writing is not None
        else None
    )
    if doing is not None:
        raise HarvestError("busy", f"resident {resident_id!r} is {doing} and cannot do that now")


def start_harvesting(world: World, resident_id: str, spring_id: str) -> World:
    """Start a job at the spring with the best program the resident carries; they must stand
    within its radius and not be busy."""
    check_not_busy(world, resident_id)
    resident = _find(world, resident_id)
    program = best_program(world, resident_id)
    if program is None:
        raise HarvestError("no_program", f"resident {resident_id!r} has no program")
    spring = next((spring for spring in world.springs if spring.id == spring_id), None)
    if spring is None:
        raise HarvestError("unknown_spring", f"no spring with id {spring_id!r}")
    if not spring.reaches(resident.position):
        raise HarvestError(
            "not_at_spring", f"resident {resident_id!r} is not within reach of spring {spring_id!r}"
        )
    if spring.reserve == 0:
        raise HarvestError("spring_empty", f"spring {spring_id!r} has no compute left")
    amount = min(program.compute_per_job, spring.reserve)
    job = HarvestJob(
        spring_id=spring_id,
        ends_at=world.time + HARVEST_SECONDS,
        amount=amount,
        program=program.tier,
    )
    world = _with_reserve(world, spring, spring.reserve - amount)
    return _with_job(world, resident_id, job)


def finish_harvesting(world: World) -> World:
    """Pay out every job that has ended by `world.time`; the same world when none has."""
    for resident in world.residents:
        if resident.harvesting is not None and resident.harvesting.ends_at <= world.time:
            world = store_item(world, resident.id, COMPUTE, resident.harvesting.amount)
            world = _with_job(world, resident.id, None)
    return world


def cancel_harvesting(world: World, resident_id: str) -> World:
    """Stop the resident's job, if any, and put its compute back into the spring."""
    job = _find(world, resident_id).harvesting
    if job is None:
        return world
    spring = next(spring for spring in world.springs if spring.id == job.spring_id)
    world = _with_reserve(world, spring, spring.reserve + job.amount)
    return _with_job(world, resident_id, None)


def next_finish(world: World) -> float | None:
    """Game time when the earliest running job ends, or None when nobody is harvesting."""
    ends = [r.harvesting.ends_at for r in world.residents if r.harvesting is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise HarvestError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_job(world: World, resident_id: str, job: HarvestJob | None) -> World:
    residents = tuple(
        replace(resident, harvesting=job) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def _with_reserve(world: World, spring: Spring, reserve: int) -> World:
    springs = tuple(
        replace(other, reserve=reserve) if other.id == spring.id else other
        for other in world.springs
    )
    return replace(world, springs=springs)
