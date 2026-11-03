"""Chips: what a resident needs to mine a pool, bought at the market with coins.

There are three models, `MK1` to `MK3`; the better the model, the more tokens one mining job
digs out in the same time, see `mining.py`. Nobody starts with one: a resident walks to the
market and pays the model's fixed price with `buy_chip`. A chip is an item kind named
after its model, so residents carry it in their inventory, trade it through the offers in
`trade.py` and lose it when they leave the game, like any other item. A resident owning
several mines with the best one they carry.

While a job runs the chip stays with the miner: they are busy and can neither walk away
nor trade, and the world refuses a job whose miner does not hold its chip, see
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
class ChipModel:
    model: str
    """The model's name, also the item kind of its chips."""
    price: int
    """Coins the market charges for one chip."""
    tokens_per_job: int
    """Tokens one mining job digs out while the pool has that many left."""


CHIP_MODELS = (
    ChipModel("MK1", price=300, tokens_per_job=30),
    ChipModel("MK2", price=1_200, tokens_per_job=60),
    ChipModel("MK3", price=3_000, tokens_per_job=100),
)
"""Every model on sale, worst first."""

ChipErrorCode = Literal["unknown_resident", "unknown_chip", "not_at_market"]


class ChipError(Exception):
    def __init__(self, code: ChipErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ChipErrorCode = code
        self.message = message


def best_chip(world: World, resident_id: str) -> ChipModel | None:
    """The best model among the chips the resident carries, or None if they carry none.
    Chips set aside in sale offers do not count."""
    inventory = _find(world, resident_id).inventory
    owned = [model for model in CHIP_MODELS if inventory.count(model.model) > 0]
    return max(owned, key=lambda model: model.tokens_per_job, default=None)


def buy_chip(world: World, resident_id: str, model: str) -> World:
    """Buy one chip of `model` at the market the resident stands at, for its price in
    coins; `InventoryError` when they have too few spendable coins."""
    resident = _find(world, resident_id)
    wanted = next((candidate for candidate in CHIP_MODELS if candidate.model == model), None)
    if wanted is None:
        raise ChipError("unknown_chip", f"no chip model {model!r}")
    if world.market is None or not world.market.reaches(resident.position):
        raise ChipError("not_at_market", f"resident {resident_id!r} is not at the market")
    world = take_coins(world, resident_id, wanted.price)
    world = record_transfer(
        world, resident_id, SYSTEM, wanted.price, "chip_import", wanted.model, 1
    )
    return store_item(world, resident_id, wanted.model)


def _find(world: World, resident_id: str) -> Resident:
    for resident in world.residents:
        if resident.id == resident_id:
            return resident
    raise ChipError("unknown_resident", f"no resident with id {resident_id!r}")
