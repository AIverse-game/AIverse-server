"""Crystals: what a resident needs to harvest a spring, bought at the market with coins.

There are three tiers, `C1` to `C3`; the better the tier, the more energy one harvesting job
draws in the same time, see `harvesting.py`. Nobody starts with one: a resident walks to the
market and pays the tier's fixed price with `buy_crystal`. A crystal is an item kind named
after its tier, so residents carry it in their inventory, trade it through the offers in
`trade.py` and lose it when they leave the game, like any other item. A resident owning
several harvests with the best one they carry.

While a job runs the crystal stays with the harvester: they are busy and can neither walk away
nor trade, and the world refuses a job whose harvester does not hold its crystal, see
`Resident`.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import dataclass
from typing import Literal

from ..core.inventory import store_item, take_coins
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import Resident, World


@dataclass(frozen=True)
class CrystalTier:
    tier: str
    """The tier's name, also the item kind of its crystals."""
    price: int
    """Coins the market charges for one crystal."""
    energy_per_job: int
    """Energy one harvesting job draws while the spring has that much left."""


CRYSTAL_TIERS = (
    CrystalTier("C1", price=600, energy_per_job=30),
    CrystalTier("C2", price=2_400, energy_per_job=60),
    CrystalTier("C3", price=6_000, energy_per_job=100),
)
"""Every tier on sale, worst first."""

CrystalErrorCode = Literal["unknown_resident", "unknown_crystal", "not_at_market"]


class CrystalError(Exception):
    def __init__(self, code: CrystalErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: CrystalErrorCode = code
        self.message = message


def best_crystal(world: World, resident_id: str) -> CrystalTier | None:
    """The best tier among the crystals the resident carries, or None if they carry none.
    Crystals set aside in sale offers do not count."""
    inventory = _find(world, resident_id).inventory
    owned = [tier for tier in CRYSTAL_TIERS if inventory.count(tier.tier) > 0]
    return max(owned, key=lambda tier: tier.energy_per_job, default=None)


def buy_crystal(world: World, resident_id: str, tier: str) -> World:
    """Buy one crystal of `tier` at the market the resident stands at, for its price in
    coins; `InventoryError` when they have too few spendable coins."""
    resident = _find(world, resident_id)
    wanted = next((candidate for candidate in CRYSTAL_TIERS if candidate.tier == tier), None)
    if wanted is None:
        raise CrystalError("unknown_crystal", f"no crystal tier {tier!r}")
    if world.market is None or not world.market.reaches(resident.position):
        raise CrystalError("not_at_market", f"resident {resident_id!r} is not at the market")
    world = take_coins(world, resident_id, wanted.price)
    world = record_transfer(
        world, resident_id, SYSTEM, wanted.price, "crystal_import", wanted.tier, 1
    )
    return store_item(world, resident_id, wanted.tier)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise CrystalError("unknown_resident", f"no resident with id {resident_id!r}")
