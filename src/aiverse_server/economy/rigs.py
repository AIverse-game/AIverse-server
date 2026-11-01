"""Shared finite-pool mining: installed chips work while their residents remain in play.

Only prepaid coins pay rent and electricity. A cycle either pays and produces in full
(limited by the pool) or does nothing. No future production counts as stamina.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import add_coins, store_item, take_coins, take_item
from ..core.rigs import RIG_CAPACITY as RIG_CAPACITY
from ..core.rigs import MiningRig, RigSlot
from ..core.world import Pool, World
from .chips import CHIP_MODELS
from .tokens import TOKEN

RIG_PRICE = 8_000
RIG_SECONDS = 20.0
RIG_YIELD = 2
"""A chip in a rig mines this many times its tokens per job each cycle."""
RENT_PER_100 = 50
ENERGY_PER_100 = 20

RigErrorCode = Literal[
    "unknown_rig",
    "unknown_slot",
    "rig_full",
    "unknown_chip",
    "unknown_pool",
    "not_at_pool",
    "pool_empty",
    "invalid_deposit",
]


class RigError(Exception):
    def __init__(self, code: RigErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_rig(world: World, resident_id: str, pool_id: str) -> World:
    pool = _at_pool(world, resident_id, pool_id)
    if pool.reserve == 0:
        raise RigError("pool_empty", "the pool is empty")
    world = take_coins(world, resident_id, RIG_PRICE)
    rig = MiningRig(world.next_rig_id, resident_id, pool_id)
    return replace(world, rigs=(*world.rigs, rig), next_rig_id=world.next_rig_id + 1)


def install_chip(world: World, resident_id: str, rig_id: int, chip: str, deposit: int) -> World:
    _check_deposit(deposit)
    rig = _rig(world, rig_id)
    _at_pool(world, resident_id, rig.pool_id, f"rig {rig.id} is at pool {rig.pool_id}; stand there")
    if len(rig.slots) >= RIG_CAPACITY:
        raise RigError("rig_full", "the rig has no free slots")
    if chip not in {model.model for model in CHIP_MODELS}:
        raise RigError("unknown_chip", "unknown chip model")
    world = take_item(world, resident_id, chip)
    world = take_coins(world, resident_id, deposit)
    slot = RigSlot(rig.next_slot_id, resident_id, chip, deposit, world.time + RIG_SECONDS)
    return _with_rig(world, replace(rig, slots=(*rig.slots, slot), next_slot_id=slot.id + 1))


def fund_slot(world: World, resident_id: str, rig_id: int, slot_id: int, amount: int) -> World:
    _check_deposit(amount)
    rig = _rig(world, rig_id)
    slot = _slot(rig, resident_id, slot_id)
    was_paused = slot_status(world, rig, slot) != "running"
    world = take_coins(world, resident_id, amount)
    funded = replace(
        slot,
        balance=slot.balance + amount,
        next_at=world.time + RIG_SECONDS if was_paused else slot.next_at,
    )
    return _with_slot(world, rig, funded)


def remove_chip(world: World, resident_id: str, rig_id: int, slot_id: int) -> World:
    rig = _rig(world, rig_id)
    slot = _slot(rig, resident_id, slot_id)
    world = _with_rig(world, replace(rig, slots=tuple(s for s in rig.slots if s.id != slot_id)))
    world = store_item(world, resident_id, slot.chip)
    return add_coins(world, resident_id, slot.balance) if slot.balance else world


def release_rigs(world: World, resident_id: str | None = None) -> World:
    """Release all slots on game end, or the leaving resident's slots and owned machines."""
    for rig in world.rigs:
        for slot in rig.slots:
            if resident_id is None or resident_id in (rig.owner_id, slot.resident_id):
                world = remove_chip(world, slot.resident_id, rig.id, slot.id)
    if resident_id is not None:
        world = replace(world, rigs=tuple(r for r in world.rigs if r.owner_id != resident_id))
    return world


def rig_coins(world: World, resident_id: str) -> int:
    return sum(s.balance for r in world.rigs for s in r.slots if s.resident_id == resident_id)


def cycle_amount(world: World, rig: MiningRig, slot: RigSlot) -> int:
    pool = next(pool for pool in world.pools if pool.id == rig.pool_id)
    model = next(model for model in CHIP_MODELS if model.model == slot.chip)
    return min(pool.reserve, model.tokens_per_job * RIG_YIELD)


def pool_draw_rate(world: World, pool_id: str) -> float:
    """Tokens per second the running rig slots on a pool take out of it; paused slots and
    manual mining are not counted."""
    return sum(
        cycle_amount(world, rig, slot) / RIG_SECONDS
        for rig in world.rigs
        if rig.pool_id == pool_id
        for slot in rig.slots
        if slot_status(world, rig, slot) == "running"
    )


def cycle_cost(rig: MiningRig, slot: RigSlot, amount: int) -> tuple[int, int]:
    rent = 0 if rig.owner_id == slot.resident_id else (amount * RENT_PER_100 + 99) // 100
    energy = (amount * ENERGY_PER_100 + 99) // 100
    return rent, energy


def slot_status(
    world: World, rig: MiningRig, slot: RigSlot
) -> Literal["running", "no_funds", "pool_empty"]:
    amount = cycle_amount(world, rig, slot)
    if amount == 0:
        return "pool_empty"
    return "running" if sum(cycle_cost(rig, slot, amount)) <= slot.balance else "no_funds"


def finish_rigs(world: World) -> World:
    """Settle cycles due now. The game clock calls in chronological order, not in bulk."""
    if world.ended:
        return world
    for original in world.rigs:
        for old_slot in original.slots:
            if old_slot.next_at > world.time:
                continue
            rig = _rig(world, original.id)
            slot = next(s for s in rig.slots if s.id == old_slot.id)
            amount = cycle_amount(world, rig, slot)
            rent, energy = cycle_cost(rig, slot, amount)
            if amount and slot.balance >= rent + energy:
                world = replace(
                    world,
                    pools=tuple(
                        replace(p, reserve=p.reserve - amount) if p.id == rig.pool_id else p
                        for p in world.pools
                    ),
                )
                world = store_item(world, slot.resident_id, TOKEN, amount)
                if rent:
                    world = add_coins(world, rig.owner_id, rent)
                slot = replace(
                    slot,
                    balance=slot.balance - rent - energy,
                    mined=slot.mined + amount,
                    rent_paid=slot.rent_paid + rent,
                )
            world = _with_slot(world, rig, replace(slot, next_at=world.time + RIG_SECONDS))
    return world


def next_cycle(world: World) -> float | None:
    return min((s.next_at for r in world.rigs for s in r.slots), default=None)


def _check_deposit(amount: int) -> None:
    if type(amount) is not int or amount < 1:
        raise RigError("invalid_deposit", "prepayment must be a positive integer")


def _at_pool(
    world: World,
    resident_id: str,
    pool_id: str,
    refusal: str = "stand within the pool to build or install",
) -> Pool:
    pool = next((p for p in world.pools if p.id == pool_id), None)
    if pool is None:
        raise RigError("unknown_pool", "unknown pool")
    resident = next(r for r in world.residents if r.id == resident_id)
    if not pool.reaches(resident.position):
        raise RigError("not_at_pool", refusal)
    return pool


def _rig(world: World, rig_id: int) -> MiningRig:
    rig = next((r for r in world.rigs if r.id == rig_id), None)
    if rig is None:
        raise RigError("unknown_rig", "unknown rig")
    return rig


def _slot(rig: MiningRig, resident_id: str, slot_id: int) -> RigSlot:
    slot = next((s for s in rig.slots if s.id == slot_id and s.resident_id == resident_id), None)
    if slot is None:
        raise RigError("unknown_slot", "no slot owned by this resident")
    return slot


def _with_rig(world: World, rig: MiningRig) -> World:
    return replace(world, rigs=tuple(rig if r.id == rig.id else r for r in world.rigs))


def _with_slot(world: World, rig: MiningRig, slot: RigSlot) -> World:
    return _with_rig(
        world, replace(rig, slots=tuple(slot if s.id == slot.id else s for s in rig.slots))
    )
