"""Programs: what a resident needs to harvest a spring, bought at the market with coins.

There are three tiers, `v1` to `v3`; the better the tier, the more compute one harvesting job
draws in the same time, see `harvesting.py`. Nobody starts with one: a resident walks to the
market and pays the tier's fixed price with `buy_program`. A program is an item kind named
after its tier, so residents carry it in their inventory, trade it through the offers in
`trade.py` and lose it when they leave the game, like any other item. A resident owning
several harvests with the best one they carry.

While a job runs the program stays with the harvester: they are busy and can neither walk away
nor trade, and the world refuses a job whose harvester does not hold its program, see
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
class ProgramTier:
    tier: str
    """The tier's name, also the item kind of its programs."""
    price: int
    """Coins the market charges for one program."""
    compute_per_job: int
    """Compute one harvesting job draws while the spring has that much left."""


PROGRAM_TIERS = (
    ProgramTier("v1", price=600, compute_per_job=30),
    ProgramTier("v2", price=2_400, compute_per_job=60),
    ProgramTier("v3", price=6_000, compute_per_job=100),
)
"""Every tier on sale, worst first."""

ProgramErrorCode = Literal["unknown_resident", "unknown_program", "not_at_market"]


class ProgramError(Exception):
    def __init__(self, code: ProgramErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ProgramErrorCode = code
        self.message = message


def best_program(world: World, resident_id: str) -> ProgramTier | None:
    """The best tier among the programs the resident carries, or None if they carry none.
    Programs set aside in sale offers do not count."""
    inventory = _find(world, resident_id).inventory
    owned = [tier for tier in PROGRAM_TIERS if inventory.count(tier.tier) > 0]
    return max(owned, key=lambda tier: tier.compute_per_job, default=None)


def buy_program(world: World, resident_id: str, tier: str) -> World:
    """Buy one program of `tier` at the market the resident stands at, for its price in
    coins; `InventoryError` when they have too few spendable coins."""
    resident = _find(world, resident_id)
    wanted = next((candidate for candidate in PROGRAM_TIERS if candidate.tier == tier), None)
    if wanted is None:
        raise ProgramError("unknown_program", f"no program tier {tier!r}")
    if world.market is None or not world.market.reaches(resident.position):
        raise ProgramError("not_at_market", f"resident {resident_id!r} is not at the market")
    world = take_coins(world, resident_id, wanted.price)
    world = record_transfer(
        world, resident_id, SYSTEM, wanted.price, "program_import", wanted.tier, 1
    )
    return store_item(world, resident_id, wanted.tier)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise ProgramError("unknown_resident", f"no resident with id {resident_id!r}")
