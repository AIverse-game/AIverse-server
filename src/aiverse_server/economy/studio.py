"""The studio: where residents write programs out of data and compute.

Each tier has a recipe, `RECIPES`: data, compute and, above the first tier, one program of
the tier below, all taken from what the resident carries (not what offers set aside) when the
job starts. The job lasts the recipe's seconds, less `SPEEDUP_PER_PROGRAM` of them for every
program the resident has written this game, down to `FASTEST_SHARE`; until it ends they are
busy, as a harvester is, see `harvesting.check_not_busy`, and then get the program. Leaving the
game or the game ending drops the job, and the materials with it: leaving clears every item
anyway, and items count for nothing once the game is over.

The market also sells programs, see `programs.py`, at a price that leaves room for writing.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import dataclass, replace
from typing import Literal

from ..core.inventory import item_count, store_item, take_item
from ..core.world import Resident, World, WriteJob
from .compute import COMPUTE
from .gathering import DATA


@dataclass(frozen=True)
class Recipe:
    tier: str
    """The program it makes."""
    data: int
    compute: int
    consumes: str | None
    """Tier of the program it uses up, None for the first tier."""
    seconds: float
    """Game seconds the job takes for a resident who has written nothing yet."""


RECIPES = (
    Recipe("v1", data=5, compute=15, consumes=None, seconds=15.0),
    Recipe("v2", data=15, compute=40, consumes="v1", seconds=30.0),
    Recipe("v3", data=30, compute=80, consumes="v2", seconds=45.0),
)
"""One per program tier, worst first."""

SPEEDUP_PER_PROGRAM = 0.1
"""Share of a recipe's seconds every program written so far takes off the next job."""

FASTEST_SHARE = 0.5
"""Least share of a recipe's seconds a job takes, however practised the resident."""

StudioErrorCode = Literal[
    "unknown_resident",
    "unknown_program",
    "not_at_studio",
    "not_enough_data",
    "not_enough_compute",
    "no_lower_program",
]


class StudioError(Exception):
    def __init__(self, code: StudioErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: StudioErrorCode = code
        self.message = message


def write_seconds(recipe: Recipe, programs_written: int) -> float:
    """Game seconds the recipe takes a resident who has written `programs_written` programs."""
    return recipe.seconds * max(1 - SPEEDUP_PER_PROGRAM * programs_written, FASTEST_SHARE)


def start_writing(world: World, resident_id: str, tier: str) -> World:
    """Spend the recipe's materials and start writing a program of `tier`; the resident must
    stand at the studio, and the caller checks they are not busy, see `actions.perform`."""
    resident = _find(world, resident_id)
    recipe = next((recipe for recipe in RECIPES if recipe.tier == tier), None)
    if recipe is None:
        raise StudioError("unknown_program", f"no program tier {tier!r}")
    if world.studio is None or not world.studio.reaches(resident.position):
        raise StudioError("not_at_studio", f"resident {resident_id!r} is not at the studio")
    needs: list[tuple[str, int, StudioErrorCode]] = [
        (DATA, recipe.data, "not_enough_data"),
        (COMPUTE, recipe.compute, "not_enough_compute"),
    ]
    if recipe.consumes is not None:
        needs.append((recipe.consumes, 1, "no_lower_program"))
    for kind, count, code in needs:
        held = item_count(world, resident_id, kind)
        if held < count:
            raise StudioError(
                code, f"{tier} takes {count} {kind}, resident {resident_id!r} carries {held}"
            )
    for kind, count, _ in needs:
        world = take_item(world, resident_id, kind, count)
    job = WriteJob(tier, world.time + write_seconds(recipe, resident.programs_written))
    return _with_job(world, resident_id, job, resident.programs_written)


def finish_writing(world: World) -> World:
    """Hand out every program whose job has ended by `world.time`; the same world when none
    has."""
    for resident in world.residents:
        job = resident.writing
        if job is not None and job.ends_at <= world.time:
            world = store_item(world, resident.id, job.tier)
            world = _with_job(world, resident.id, None, resident.programs_written + 1)
    return world


def cancel_writing(world: World, resident_id: str) -> World:
    """Drop the resident's job, if any; its materials are not handed back."""
    resident = _find(world, resident_id)
    if resident.writing is None:
        return world
    return _with_job(world, resident_id, None, resident.programs_written)


def next_write_end(world: World) -> float | None:
    """Game time when the earliest running job ends, or None when nobody is writing."""
    ends = [r.writing.ends_at for r in world.residents if r.writing is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise StudioError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_job(world: World, resident_id: str, job: WriteJob | None, written: int) -> World:
    residents = tuple(
        replace(resident, writing=job, programs_written=written)
        if resident.id == resident_id
        else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
