"""Mining: residents spend time at a token pool to earn tokens.

A resident standing within a pool's radius starts a job that lasts `MINE_SECONDS` of game
time. They need a chip and use the best one they carry, see `chips.py`. The
tokens, as many as that model digs per job or whatever the pool has left if less, come out
of the pool at once, so two miners cannot dig out the same tokens, and reach the miner when
the job ends. Until then the miner is busy and stays with the chip: they can do nothing
but talk, or leave the game, which puts the tokens back into the pool. Walkers are busy the
same way, see `walking.py`; `check_not_busy` covers both.

Game time is `World.time`. The server moves it on as real time passes and calls
`finish_mining` to pay out the jobs that are due; `next_finish` tells it when to look again.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import store_item
from ..core.world import MiningJob, Pool, Resident, World
from .chips import best_chip
from .tokens import TOKEN

MINE_SECONDS = 10.0
"""Game seconds one mining job takes."""

MiningErrorCode = Literal[
    "unknown_resident", "unknown_pool", "not_at_pool", "pool_empty", "busy", "no_chip"
]


class MiningError(Exception):
    def __init__(self, code: MiningErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: MiningErrorCode = code
        self.message = message


def check_not_busy(world: World, resident_id: str) -> None:
    """Refuse a resident who is in the middle of a mining job or a walk."""
    resident = _find(world, resident_id)
    if resident.mining is not None:
        raise MiningError("busy", f"resident {resident_id!r} is mining and cannot do that now")
    if resident.walking is not None:
        raise MiningError("busy", f"resident {resident_id!r} is walking and cannot do that now")


def start_mining(world: World, resident_id: str, pool_id: str) -> World:
    """Start a job at the pool with the best chip the resident carries; they must stand
    within its radius and not be busy."""
    check_not_busy(world, resident_id)
    resident = _find(world, resident_id)
    chip = best_chip(world, resident_id)
    if chip is None:
        raise MiningError("no_chip", f"resident {resident_id!r} has no chip")
    pool = next((pool for pool in world.pools if pool.id == pool_id), None)
    if pool is None:
        raise MiningError("unknown_pool", f"no pool with id {pool_id!r}")
    if not pool.reaches(resident.position):
        raise MiningError(
            "not_at_pool", f"resident {resident_id!r} is not within reach of pool {pool_id!r}"
        )
    if pool.reserve == 0:
        raise MiningError("pool_empty", f"pool {pool_id!r} has no tokens left")
    amount = min(chip.tokens_per_job, pool.reserve)
    job = MiningJob(
        pool_id=pool_id, ends_at=world.time + MINE_SECONDS, amount=amount, chip=chip.model
    )
    world = _with_reserve(world, pool, pool.reserve - amount)
    return _with_job(world, resident_id, job)


def finish_mining(world: World) -> World:
    """Pay out every job that has ended by `world.time`; the same world when none has."""
    for resident in world.residents:
        if resident.mining is not None and resident.mining.ends_at <= world.time:
            world = store_item(world, resident.id, TOKEN, resident.mining.amount)
            world = _with_job(world, resident.id, None)
    return world


def cancel_mining(world: World, resident_id: str) -> World:
    """Stop the resident's job, if any, and put its tokens back into the pool."""
    job = _find(world, resident_id).mining
    if job is None:
        return world
    pool = next(pool for pool in world.pools if pool.id == job.pool_id)
    world = _with_reserve(world, pool, pool.reserve + job.amount)
    return _with_job(world, resident_id, None)


def next_finish(world: World) -> float | None:
    """Game time when the earliest running job ends, or None when nobody is mining."""
    ends = [r.mining.ends_at for r in world.residents if r.mining is not None]
    return min(ends, default=None)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise MiningError("unknown_resident", f"no resident with id {resident_id!r}")


def _with_job(world: World, resident_id: str, job: MiningJob | None) -> World:
    residents = tuple(
        replace(resident, mining=job) if resident.id == resident_id else resident
        for resident in world.residents
    )
    return replace(world, residents=residents)


def _with_reserve(world: World, pool: Pool, reserve: int) -> World:
    pools = tuple(
        replace(other, reserve=reserve) if other.id == pool.id else other for other in world.pools
    )
    return replace(world, pools=pools)
