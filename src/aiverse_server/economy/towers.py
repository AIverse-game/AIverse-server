"""Shared finite-spring harvesting: installed crystals work while their residents remain in play.

Only prepaid coins pay rent and upkeep. A cycle either pays and produces in full
(limited by the spring) or does nothing. No future production counts as stamina.
"""

from dataclasses import replace
from typing import Literal

from ..core.inventory import add_coins, store_item, take_coins, take_item
from ..core.ledger import SYSTEM, record_transfer
from ..core.towers import TOWER_CAPACITY as TOWER_CAPACITY
from ..core.towers import Tower, TowerSlot
from ..core.world import Spring, World
from .crystals import CRYSTAL_TIERS
from .energy import ENERGY

TOWER_PRICE = 9_000
TOWER_SECONDS = 20.0
TOWER_YIELD = 2
"""A crystal in a tower harvests this many times its energy per job each cycle."""
RENT_PER_100 = 50
UPKEEP_PER_100 = 20

TowerErrorCode = Literal[
    "unknown_tower",
    "unknown_slot",
    "tower_full",
    "unknown_crystal",
    "unknown_spring",
    "not_at_spring",
    "spring_empty",
    "invalid_deposit",
]


class TowerError(Exception):
    def __init__(self, code: TowerErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_tower(world: World, resident_id: str, spring_id: str) -> World:
    spring = _at_spring(world, resident_id, spring_id)
    if spring.reserve == 0:
        raise TowerError("spring_empty", "the spring is empty")
    world = take_coins(world, resident_id, TOWER_PRICE)
    world = record_transfer(world, resident_id, SYSTEM, TOWER_PRICE, "tower_build")
    tower = Tower(world.next_tower_id, resident_id, spring_id)
    return replace(world, towers=(*world.towers, tower), next_tower_id=world.next_tower_id + 1)


def install_crystal(
    world: World, resident_id: str, tower_id: int, crystal: str, deposit: int
) -> World:
    _check_deposit(deposit)
    tower = _tower(world, tower_id)
    _at_spring(
        world,
        resident_id,
        tower.spring_id,
        f"tower {tower.id} is at spring {tower.spring_id}; stand there",
    )
    if len(tower.slots) >= TOWER_CAPACITY:
        raise TowerError("tower_full", "the tower has no free slots")
    if crystal not in {tier.tier for tier in CRYSTAL_TIERS}:
        raise TowerError("unknown_crystal", "unknown crystal tier")
    world = take_item(world, resident_id, crystal)
    world = take_coins(world, resident_id, deposit)
    slot = TowerSlot(tower.next_slot_id, resident_id, crystal, deposit, world.time + TOWER_SECONDS)
    return _with_tower(world, replace(tower, slots=(*tower.slots, slot), next_slot_id=slot.id + 1))


def fund_slot(world: World, resident_id: str, tower_id: int, slot_id: int, amount: int) -> World:
    _check_deposit(amount)
    tower = _tower(world, tower_id)
    slot = _slot(tower, resident_id, slot_id)
    was_paused = slot_status(world, tower, slot) != "running"
    world = take_coins(world, resident_id, amount)
    funded = replace(
        slot,
        balance=slot.balance + amount,
        next_at=world.time + TOWER_SECONDS if was_paused else slot.next_at,
    )
    return _with_slot(world, tower, funded)


def remove_crystal(world: World, resident_id: str, tower_id: int, slot_id: int) -> World:
    tower = _tower(world, tower_id)
    slot = _slot(tower, resident_id, slot_id)
    world = _with_tower(
        world, replace(tower, slots=tuple(s for s in tower.slots if s.id != slot_id))
    )
    world = store_item(world, resident_id, slot.crystal)
    return add_coins(world, resident_id, slot.balance) if slot.balance else world


def release_towers(world: World, resident_id: str | None = None) -> World:
    """Release all slots on game end, or the leaving resident's slots and owned towers."""
    for tower in world.towers:
        for slot in tower.slots:
            if resident_id is None or resident_id in (tower.owner_id, slot.resident_id):
                world = remove_crystal(world, slot.resident_id, tower.id, slot.id)
    if resident_id is not None:
        world = replace(world, towers=tuple(r for r in world.towers if r.owner_id != resident_id))
    return world


def tower_coins(world: World, resident_id: str) -> int:
    return sum(s.balance for r in world.towers for s in r.slots if s.resident_id == resident_id)


def cycle_amount(world: World, tower: Tower, slot: TowerSlot) -> int:
    spring = next(spring for spring in world.springs if spring.id == tower.spring_id)
    tier = next(tier for tier in CRYSTAL_TIERS if tier.tier == slot.crystal)
    return min(spring.reserve, tier.energy_per_job * TOWER_YIELD)


def spring_draw_rate(world: World, spring_id: str) -> float:
    """Energy per second the running tower slots on a spring take out of it; paused slots and
    manual harvesting are not counted."""
    return sum(
        cycle_amount(world, tower, slot) / TOWER_SECONDS
        for tower in world.towers
        if tower.spring_id == spring_id
        for slot in tower.slots
        if slot_status(world, tower, slot) == "running"
    )


def cycle_cost(tower: Tower, slot: TowerSlot, amount: int) -> tuple[int, int]:
    rent = 0 if tower.owner_id == slot.resident_id else (amount * RENT_PER_100 + 99) // 100
    upkeep = (amount * UPKEEP_PER_100 + 99) // 100
    return rent, upkeep


def slot_status(
    world: World, tower: Tower, slot: TowerSlot
) -> Literal["running", "no_funds", "spring_empty"]:
    amount = cycle_amount(world, tower, slot)
    if amount == 0:
        return "spring_empty"
    return "running" if sum(cycle_cost(tower, slot, amount)) <= slot.balance else "no_funds"


def finish_towers(world: World) -> World:
    """Settle cycles due now. The game clock calls in chronological order, not in bulk."""
    if world.ended:
        return world
    for original in world.towers:
        for old_slot in original.slots:
            if old_slot.next_at > world.time:
                continue
            tower = _tower(world, original.id)
            slot = next(s for s in tower.slots if s.id == old_slot.id)
            amount = cycle_amount(world, tower, slot)
            rent, upkeep = cycle_cost(tower, slot, amount)
            if amount and slot.balance >= rent + upkeep:
                world = replace(
                    world,
                    springs=tuple(
                        replace(p, reserve=p.reserve - amount) if p.id == tower.spring_id else p
                        for p in world.springs
                    ),
                )
                world = store_item(world, slot.resident_id, ENERGY, amount)
                if rent:
                    world = add_coins(world, tower.owner_id, rent)
                    # No item: the energy come from the spring, the rent only pays for the tower.
                    world = record_transfer(world, slot.resident_id, tower.owner_id, rent, "rent")
                if upkeep:
                    world = record_transfer(world, slot.resident_id, SYSTEM, upkeep, "upkeep")
                slot = replace(
                    slot,
                    balance=slot.balance - rent - upkeep,
                    harvested=slot.harvested + amount,
                    rent_paid=slot.rent_paid + rent,
                )
            world = _with_slot(world, tower, replace(slot, next_at=world.time + TOWER_SECONDS))
    return world


def next_cycle(world: World) -> float | None:
    return min((s.next_at for r in world.towers for s in r.slots), default=None)


def _check_deposit(amount: int) -> None:
    if type(amount) is not int or amount < 1:
        raise TowerError("invalid_deposit", "prepayment must be a positive integer")


def _at_spring(
    world: World,
    resident_id: str,
    spring_id: str,
    refusal: str = "stand within the spring to build or install",
) -> Spring:
    spring = next((p for p in world.springs if p.id == spring_id), None)
    if spring is None:
        raise TowerError("unknown_spring", "unknown spring")
    resident = next(r for r in world.residents if r.id == resident_id)
    if not spring.reaches(resident.position):
        raise TowerError("not_at_spring", refusal)
    return spring


def _tower(world: World, tower_id: int) -> Tower:
    tower = next((r for r in world.towers if r.id == tower_id), None)
    if tower is None:
        raise TowerError("unknown_tower", "unknown tower")
    return tower


def _slot(tower: Tower, resident_id: str, slot_id: int) -> TowerSlot:
    slot = next((s for s in tower.slots if s.id == slot_id and s.resident_id == resident_id), None)
    if slot is None:
        raise TowerError("unknown_slot", "no slot owned by this resident")
    return slot


def _with_tower(world: World, tower: Tower) -> World:
    return replace(world, towers=tuple(tower if r.id == tower.id else r for r in world.towers))


def _with_slot(world: World, tower: Tower, slot: TowerSlot) -> World:
    return _with_tower(
        world, replace(tower, slots=tuple(slot if s.id == slot.id else s for s in tower.slots))
    )
