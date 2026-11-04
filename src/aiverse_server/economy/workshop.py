"""The workshop: where residents refine crystals out of shards and energy.

Each tier has a recipe, `RECIPES`: shards, energy and, above the first tier, one crystal of
the tier below, all taken from what the resident carries (not what offers set aside) when the
job starts. The job lasts the recipe's seconds, less `SPEEDUP_PER_CRYSTAL` of them for every
crystal the resident has refined this game, down to `FASTEST_SHARE`; until it ends they are
busy, as a harvester is, see `harvesting.check_not_busy`, and then get the crystal. Leaving the
game or the game ending drops the job, and the materials with it: leaving clears every item
anyway, and items count for nothing once the game is over.

The market also sells crystals, see `crystals.py`, at a price that leaves room for refining.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import dataclass, replace
from typing import Literal

from ..core.inventory import item_count, store_item, take_item
from ..core.world import RefineJob, Resident, World
from .energy import ENERGY
from .shards import SHARD


@dataclass(frozen=True)
class Recipe:
    tier: str
    """The crystal it makes."""
    shards: int
    energy: int
    consumes: str | None
    """Tier of the crystal it uses up, None for the first tier."""
    seconds: float
    """Game seconds the job takes for a resident who has refined nothing yet."""


RECIPES = (
    Recipe("C1", shards=5, energy=15, consumes=None, seconds=15.0),
    Recipe("C2", shards=15, energy=40, consumes="C1", seconds=30.0),
    Recipe("C3", shards=30, energy=80, consumes="C2", seconds=45.0),
)
"""One per crystal tier, worst first."""

SPEEDUP_PER_CRYSTAL = 0.1
"""Share of a recipe's seconds every crystal refined so far takes off the next job."""

FASTEST_SHARE = 0.5
"""Least share of a recipe's seconds a job takes, however practised the resident."""

WorkshopErrorCode = Literal[
    "unknown_resident",
    "unknown_crystal",
    "not_at_workshop",
    "not_enough_shards",
    "not_enough_energy",
    "no_lower_crystal",
]


class WorkshopError(Exception):
    def __init__(self, code: WorkshopErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: WorkshopErrorCode = code
        self.message = message


def refine_seconds(recipe: Recipe, crystals_refined: int) -> float:
    """Game seconds the recipe takes a resident who has refined `crystals_refined` crystals."""
    return recipe.seconds * max(1 - SPEEDUP_PER_CRYSTAL * crystals_refined, FASTEST_SHARE)


def start_refining(world: World, resident_id: str, tier: str) -> World:
    """Spend the recipe's materials and start refining a crystal of `tier`; the resident must
    stand at the workshop, and the caller checks they are not busy, see `actions.perform`."""
    resident = _find(world, resident_id)
    recipe = next((recipe for recipe in RECIPES if recipe.tier == tier), None)
    if recipe is None:
        raise WorkshopError("unknown_crystal", f"no crystal tier {tier!r}")
    if world.workshop is None or not world.workshop.reaches(resident.position):
        raise WorkshopError("not_at_workshop", f"resident {resident_id!r} is not at the workshop")
    needs: list[tuple[str, int, WorkshopErrorCode]] = [
        (SHARD, recipe.shards, "not_enough_shards"),
        (ENERGY, recipe.energy, "not_enough_energy"),
    ]
    if recipe.consumes is not None:
        needs.append((recipe.consumes, 1, "no_lower_crystal"))
    for kind, count, code in needs:
        held = item_count(world, resident_id, kind)
        if held < count:
            raise WorkshopError(
                code, f"{tier} takes {count} {kind}, resident {resident_id!r} carries {held}"
            )
    for kind, count, _ in needs:
        world = take_item(world, resident_id, kind, count)
    job = RefineJob(tier, world.time + refine_seconds(recipe, resident.crystals_refined))
    return _with_job(world, resident_id, job, resident.crystals_refined)


def finish_refining(world: World) -> World:
    """Hand out every crystal whose job has ended by `world.time`; the same world when none
    has."""
    for resident in world.residents:
        job = resident.refining
        if job is not None and job.ends_at <= world.time:
            world = store_item(world, resident.id, job.tier)
            world = _with_job(world, resident.id, None, resident.crystals_refined + 1)
    return world


def cancel_refining(world: World, resident_id: str) -> World:
    """Drop the resident's job, if any; its materials are not handed back."""
    resident = _find(world, resident_id)
    if resident.refining is None:
        return world
    return _with_job(world, resident_id, None, resident.crystals_refined)


def next_refine_end(world: World) -> float | None:
    """Game time when the earliest running job ends, or None when nobody is refining."""
    ends = [r.refining.ends_at for r in world.residents if r.refining is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise WorkshopError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_job(world: World, resident_id: str, job: RefineJob | None, refined: int) -> World:
    residents = tuple(
        replace(resident, refining=job, crystals_refined=refined)
        if resident.id == resident_id
        else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)
