"""Shards: what crystals are refined from, dug by hand at quarries.

Shards are an item kind, `SHARD`, that the market neither buys nor sells, so they are worth
only what other residents pay for them; the workshop turns them into crystals, see
`workshop.py`. A resident standing within a quarry's radius starts a job that lasts
`DIG_SECONDS` of game time and needs no crystal. The shards, `DIG_YIELD` or whatever the
quarry has left if less, come out of the quarry at once, so two diggers cannot dig the same
shards, and reach the digger when the job ends. Until then the digger is busy, as a harvester
is, see `harvesting.check_not_busy`; leaving the game or the game ending puts the shards back.
Towers cannot dig: digging is always someone's time.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import store_item
from ..core.world import DigJob, Quarry, Resident, World

SHARD = "shard"
"""The item kind of shards."""

DIG_SECONDS = 10.0
"""Game seconds one digging job takes."""

DIG_YIELD = 6
"""Shards one digging job takes out while the quarry has that many left."""

ShardErrorCode = Literal["unknown_resident", "unknown_quarry", "not_at_quarry", "quarry_empty"]


class ShardError(Exception):
    def __init__(self, code: ShardErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ShardErrorCode = code
        self.message = message


def start_digging(world: World, resident_id: str, quarry_id: str) -> World:
    """Start a job at the quarry, which the resident must stand within the radius of; the
    caller checks they are not busy, see `actions.perform`."""
    resident = _find(world, resident_id)
    quarry = next((quarry for quarry in world.quarries if quarry.id == quarry_id), None)
    if quarry is None:
        raise ShardError("unknown_quarry", f"no quarry with id {quarry_id!r}")
    if not quarry.reaches(resident.position):
        raise ShardError(
            "not_at_quarry", f"resident {resident_id!r} is not within reach of quarry {quarry_id!r}"
        )
    if quarry.reserve == 0:
        raise ShardError("quarry_empty", f"quarry {quarry_id!r} has no shards left")
    amount = min(DIG_YIELD, quarry.reserve)
    job = DigJob(quarry_id=quarry_id, ends_at=world.time + DIG_SECONDS, amount=amount)
    world = _with_reserve(world, quarry, quarry.reserve - amount)
    return _with_job(world, resident_id, job)


def finish_digging(world: World) -> World:
    """Pay out every job that has ended by `world.time`; the same world when none has."""
    for resident in world.residents:
        if resident.digging is not None and resident.digging.ends_at <= world.time:
            world = store_item(world, resident.id, SHARD, resident.digging.amount)
            world = _with_job(world, resident.id, None)
    return world


def cancel_digging(world: World, resident_id: str) -> World:
    """Stop the resident's job, if any, and put its shards back into the quarry."""
    job = _find(world, resident_id).digging
    if job is None:
        return world
    quarry = next(quarry for quarry in world.quarries if quarry.id == job.quarry_id)
    world = _with_reserve(world, quarry, quarry.reserve + job.amount)
    return _with_job(world, resident_id, None)


def next_dig_end(world: World) -> float | None:
    """Game time when the earliest running job ends, or None when nobody is digging."""
    ends = [r.digging.ends_at for r in world.residents if r.digging is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise ShardError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_job(world: World, resident_id: str, job: DigJob | None) -> World:
    residents = tuple(
        replace(resident, digging=job) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def _with_reserve(world: World, quarry: Quarry, reserve: int) -> World:
    quarries = tuple(
        replace(other, reserve=reserve) if other.id == quarry.id else other
        for other in world.quarries
    )
    return replace(world, quarries=quarries)
