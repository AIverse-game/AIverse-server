"""Shared finite-spring harvesting: installed programs work while their residents remain in play.

Only prepaid coins pay rent and upkeep. A cycle either pays and produces in full
(limited by the spring) or does nothing. No future production counts as stamina.
"""

from dataclasses import replace
from typing import Literal

from ..core.collectors import COLLECTOR_CAPACITY as COLLECTOR_CAPACITY
from ..core.collectors import Collector, CollectorSlot
from ..core.inventory import add_coins, store_item, take_coins, take_item
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import Spring, World
from .compute import COMPUTE
from .programs import PROGRAM_TIERS

COLLECTOR_PRICE = 9_000
COLLECTOR_SECONDS = 20.0
COLLECTOR_YIELD = 2
"""A program in a collector harvests this many times its compute per job each cycle."""
RENT_PER_100 = 50
UPKEEP_PER_100 = 20

CollectorErrorCode = Literal[
    "unknown_collector",
    "unknown_slot",
    "collector_full",
    "unknown_program",
    "unknown_spring",
    "not_at_spring",
    "spring_empty",
    "invalid_deposit",
]


class CollectorError(Exception):
    def __init__(self, code: CollectorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_collector(world: World, resident_id: str, spring_id: str) -> World:
    spring = _at_spring(world, resident_id, spring_id)
    if spring.reserve == 0:
        raise CollectorError("spring_empty", "the spring is empty")
    world = take_coins(world, resident_id, COLLECTOR_PRICE)
    world = record_transfer(world, resident_id, SYSTEM, COLLECTOR_PRICE, "collector_build")
    collector = Collector(world.next_collector_id, resident_id, spring_id)
    return replace(
        world,
        collectors=(*world.collectors, collector),
        next_collector_id=world.next_collector_id + 1,
    )


def install_program(
    world: World, resident_id: str, collector_id: int, program: str, deposit: int
) -> World:
    _check_deposit(deposit)
    collector = _collector(world, collector_id)
    _at_spring(
        world,
        resident_id,
        collector.spring_id,
        f"collector {collector.id} is at spring {collector.spring_id}; stand there",
    )
    if len(collector.slots) >= COLLECTOR_CAPACITY:
        raise CollectorError("collector_full", "the collector has no free slots")
    if program not in {tier.tier for tier in PROGRAM_TIERS}:
        raise CollectorError("unknown_program", "unknown program tier")
    world = take_item(world, resident_id, program)
    world = take_coins(world, resident_id, deposit)
    slot = CollectorSlot(
        collector.next_slot_id, resident_id, program, deposit, world.time + COLLECTOR_SECONDS
    )
    return _with_collector(
        world, replace(collector, slots=(*collector.slots, slot), next_slot_id=slot.id + 1)
    )


def fund_slot(
    world: World, resident_id: str, collector_id: int, slot_id: int, amount: int
) -> World:
    _check_deposit(amount)
    collector = _collector(world, collector_id)
    slot = _slot(collector, resident_id, slot_id)
    was_paused = slot_status(world, collector, slot) != "running"
    world = take_coins(world, resident_id, amount)
    funded = replace(
        slot,
        balance=slot.balance + amount,
        next_at=world.time + COLLECTOR_SECONDS if was_paused else slot.next_at,
    )
    return _with_slot(world, collector, funded)


def remove_program(world: World, resident_id: str, collector_id: int, slot_id: int) -> World:
    collector = _collector(world, collector_id)
    slot = _slot(collector, resident_id, slot_id)
    world = _with_collector(
        world, replace(collector, slots=tuple(s for s in collector.slots if s.id != slot_id))
    )
    world = store_item(world, resident_id, slot.program)
    return add_coins(world, resident_id, slot.balance) if slot.balance else world


def release_collectors(world: World, resident_id: str | None = None) -> World:
    """Release all slots on game end, or the leaving resident's slots and owned collectors."""
    for collector in world.collectors:
        for slot in collector.slots:
            if resident_id is None or resident_id in (collector.owner_id, slot.resident_id):
                world = remove_program(world, slot.resident_id, collector.id, slot.id)
    if resident_id is not None:
        world = replace(
            world, collectors=tuple(r for r in world.collectors if r.owner_id != resident_id)
        )
    return world


def collector_coins(world: World, resident_id: str) -> int:
    return sum(s.balance for r in world.collectors for s in r.slots if s.resident_id == resident_id)


def cycle_amount(world: World, collector: Collector, slot: CollectorSlot) -> int:
    spring = next(spring for spring in world.springs if spring.id == collector.spring_id)
    tier = next(tier for tier in PROGRAM_TIERS if tier.tier == slot.program)
    return min(spring.reserve, tier.compute_per_job * COLLECTOR_YIELD)


def spring_draw_rate(world: World, spring_id: str) -> float:
    """Compute per second the running collector slots on a spring take out of it; paused slots and
    manual harvesting are not counted."""
    return sum(
        cycle_amount(world, collector, slot) / COLLECTOR_SECONDS
        for collector in world.collectors
        if collector.spring_id == spring_id
        for slot in collector.slots
        if slot_status(world, collector, slot) == "running"
    )


def cycle_cost(collector: Collector, slot: CollectorSlot, amount: int) -> tuple[int, int]:
    rent = 0 if collector.owner_id == slot.resident_id else (amount * RENT_PER_100 + 99) // 100
    upkeep = (amount * UPKEEP_PER_100 + 99) // 100
    return rent, upkeep


def slot_status(
    world: World, collector: Collector, slot: CollectorSlot
) -> Literal["running", "no_funds", "spring_empty"]:
    amount = cycle_amount(world, collector, slot)
    if amount == 0:
        return "spring_empty"
    return "running" if sum(cycle_cost(collector, slot, amount)) <= slot.balance else "no_funds"


def finish_collectors(world: World) -> World:
    """Settle cycles due now. The game clock calls in chronological order, not in bulk."""
    if world.ended:
        return world
    for original in world.collectors:
        for old_slot in original.slots:
            if old_slot.next_at > world.time:
                continue
            collector = _collector(world, original.id)
            slot = next(s for s in collector.slots if s.id == old_slot.id)
            amount = cycle_amount(world, collector, slot)
            rent, upkeep = cycle_cost(collector, slot, amount)
            if amount and slot.balance >= rent + upkeep:
                world = replace(
                    world,
                    springs=tuple(
                        replace(p, reserve=p.reserve - amount) if p.id == collector.spring_id else p
                        for p in world.springs
                    ),
                )
                world = store_item(world, slot.resident_id, COMPUTE, amount)
                if rent:
                    world = add_coins(world, collector.owner_id, rent)
                    # No item: the compute comes from the spring; the rent only pays for the
                    # collector.
                    world = record_transfer(
                        world, slot.resident_id, collector.owner_id, rent, "rent"
                    )
                if upkeep:
                    world = record_transfer(world, slot.resident_id, SYSTEM, upkeep, "upkeep")
                slot = replace(
                    slot,
                    balance=slot.balance - rent - upkeep,
                    harvested=slot.harvested + amount,
                    rent_paid=slot.rent_paid + rent,
                )
            world = _with_slot(
                world, collector, replace(slot, next_at=world.time + COLLECTOR_SECONDS)
            )
    return world


def next_cycle(world: World) -> float | None:
    return min((s.next_at for r in world.collectors for s in r.slots), default=None)


def _check_deposit(amount: int) -> None:
    if type(amount) is not int or amount < 1:
        raise CollectorError("invalid_deposit", "prepayment must be a positive integer")


def _at_spring(
    world: World,
    resident_id: str,
    spring_id: str,
    refusal: str = "stand within the spring to build or install",
) -> Spring:
    spring = next((p for p in world.springs if p.id == spring_id), None)
    if spring is None:
        raise CollectorError("unknown_spring", "unknown spring")
    resident = next(r for r in world.residents if r.id == resident_id)
    if not spring.reaches(resident.position):
        raise CollectorError("not_at_spring", refusal)
    return spring


def _collector(world: World, collector_id: int) -> Collector:
    collector = next((r for r in world.collectors if r.id == collector_id), None)
    if collector is None:
        raise CollectorError("unknown_collector", "unknown collector")
    return collector


def _slot(collector: Collector, resident_id: str, slot_id: int) -> CollectorSlot:
    slot = next(
        (s for s in collector.slots if s.id == slot_id and s.resident_id == resident_id), None
    )
    if slot is None:
        raise CollectorError("unknown_slot", "no slot owned by this resident")
    return slot


def _with_collector(world: World, collector: Collector) -> World:
    return replace(
        world, collectors=tuple(collector if r.id == collector.id else r for r in world.collectors)
    )


def _with_slot(world: World, collector: Collector, slot: CollectorSlot) -> World:
    return _with_collector(
        world,
        replace(collector, slots=tuple(slot if s.id == slot.id else s for s in collector.slots)),
    )
