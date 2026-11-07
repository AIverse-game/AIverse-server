"""Data: what programs are written from, gathered by hand at mines.

Data are an item kind, `DATA`, that the market neither buys nor sells, so they are worth
only what other residents pay for them; the studio turns them into programs, see
`studio.py`. A resident standing within a mine's radius starts a job that lasts
`GATHER_SECONDS` of game time and needs no program. The data, `GATHER_YIELD` or whatever the
mine has left if less, come out of the mine at once, so two gatherers cannot gather the same
data, and reach the gatherer when the job ends. Until then the gatherer is busy, as a harvester
is, see `harvesting.check_not_busy`; leaving the game or the game ending puts the data back.
Collectors cannot gather: gathering is always someone's time.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import store_item
from ..core.world import GatherJob, Mine, Resident, World

DATA = "data"
"""The item kind of data."""

GATHER_SECONDS = 10.0
"""Game seconds one gathering job takes."""

GATHER_YIELD = 6
"""Data one gathering job takes out while the mine has that many left."""

DataErrorCode = Literal["unknown_resident", "unknown_mine", "not_at_mine", "mine_empty"]


class DataError(Exception):
    def __init__(self, code: DataErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: DataErrorCode = code
        self.message = message


def start_gathering(world: World, resident_id: str, mine_id: str) -> World:
    """Start a job at the mine, which the resident must stand within the radius of; the
    caller checks they are not busy, see `actions.perform`."""
    resident = _find(world, resident_id)
    mine = next((mine for mine in world.mines if mine.id == mine_id), None)
    if mine is None:
        raise DataError("unknown_mine", f"no mine with id {mine_id!r}")
    if not mine.reaches(resident.position):
        raise DataError(
            "not_at_mine", f"resident {resident_id!r} is not within reach of mine {mine_id!r}"
        )
    if mine.reserve == 0:
        raise DataError("mine_empty", f"mine {mine_id!r} has no data left")
    amount = min(GATHER_YIELD, mine.reserve)
    job = GatherJob(mine_id=mine_id, ends_at=world.time + GATHER_SECONDS, amount=amount)
    world = _with_reserve(world, mine, mine.reserve - amount)
    return _with_job(world, resident_id, job)


def finish_gathering(world: World) -> World:
    """Pay out every job that has ended by `world.time`; the same world when none has."""
    for resident in world.residents:
        if resident.gathering is not None and resident.gathering.ends_at <= world.time:
            world = store_item(world, resident.id, DATA, resident.gathering.amount)
            world = _with_job(world, resident.id, None)
    return world


def cancel_gathering(world: World, resident_id: str) -> World:
    """Stop the resident's job, if any, and put its data back into the mine."""
    job = _find(world, resident_id).gathering
    if job is None:
        return world
    mine = next(mine for mine in world.mines if mine.id == job.mine_id)
    world = _with_reserve(world, mine, mine.reserve + job.amount)
    return _with_job(world, resident_id, None)


def next_gather_end(world: World) -> float | None:
    """Game time when the earliest running job ends, or None when nobody is gathering."""
    ends = [r.gathering.ends_at for r in world.residents if r.gathering is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise DataError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_job(world: World, resident_id: str, job: GatherJob | None) -> World:
    residents = tuple(
        replace(resident, gathering=job) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def _with_reserve(world: World, mine: Mine, reserve: int) -> World:
    mines = tuple(
        replace(other, reserve=reserve) if other.id == mine.id else other for other in world.mines
    )
    return replace(world, mines=mines)
