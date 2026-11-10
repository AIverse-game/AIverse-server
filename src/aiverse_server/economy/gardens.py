"""Gardens: a small place of rest that raises what its whole block earns from outside the town.

A garden is a facility of one parcel, built with a generative job like an ad slot, see
`compute.py`, and cheaper than any other facility. It does two things for the owner of the land
and a third for everyone who passes:

- **A reward, once.** When the job finishes the town pays the parcel's owner `GARDEN_REWARD`
  coins, ledger channel `garden_reward`. It is paid to the owner of the parcel, not to the
  builder, and not split by shares.
- **A bonus, for as long as it stands.** Every income paid to the facilities of the garden's
  block, whatever parcel of the block it is, is raised by `GARDEN_BONUS`, a tenth: the town adds
  it on top at the same moment, as its own payment to the same holders in the same proportions,
  ledger channel `garden_bonus`. That covers what sponsors pay for views, `sponsors.py`, and the
  admission fees visitors pay, `admissions.py`, the garden's own fees included: money that comes
  from outside the town. It does not cover the tolls residents pay at portals, `portals.py`: a
  toll moves coins between residents, so a bonus on it could be farmed by teleporting back and
  forth between portals of one's own, or of a partner. A block has one bonus however many
  gardens stand in it, which is why a block may have only one garden, built or being built.
- **Visitors.** The garden is a place visitors stop at, a modest one, see `visitors.py`.

A garden shows no content, takes no commission or permit, and only the parcel's owner builds it.

Like the rest of the world, every function returns a new world and leaves its argument
untouched.
"""

from ..core.inventory import add_coins
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import World
from .land import block_parcels
from .shares import split_income

GARDEN_REWARD = 500
"""Coins the town pays the owner of a parcel when a garden on it is finished: a third of what
hiring a node to build it costs, so that building one is worth it even for those who hire."""

GARDEN_BONUS = 0.10
"""The share of an income added when its block has a garden."""


def block_has_garden(world: World, parcel_id: str) -> bool:
    """Whether a garden stands in the block of `parcel_id`; one still being built does not
    count."""
    return any(
        parcel.facility is not None and parcel.facility.kind == "garden"
        for parcel in block_parcels(world, parcel_id)
    )


def garden_planned(world: World, parcel_id: str) -> bool:
    """Whether the block of `parcel_id` has a garden, built or being built."""
    if block_has_garden(world, parcel_id):
        return True
    block = {parcel.id for parcel in block_parcels(world, parcel_id)}
    return any(job.kind == "build_garden" and job.parcel_id in block for job in world.compute.jobs)


def income_multiplier(world: World, parcel_id: str) -> float:
    """What incomes on `parcel_id` are multiplied by: more than 1 when its block has a garden."""
    return 1.0 + GARDEN_BONUS if block_has_garden(world, parcel_id) else 1.0


def bonus_on(world: World, parcel_id: str, amount: float) -> int:
    """Whole coins the garden adds to an income of `amount` coins on `parcel_id`: the rounded
    boosted income less the rounded income, so that the rounding of the two never piles up into
    more or less than a tenth. 0 without a garden in the block."""
    multiplier = income_multiplier(world, parcel_id)
    if multiplier == 1.0:
        return 0
    return max(round(amount * multiplier) - round(amount), 0)


def pay_bonus(world: World, parcel_id: str, bonus: int) -> World:
    """Pay the holders of the parcel `bonus` coins from the town, in proportion to their
    shares."""
    if bonus <= 0:
        return world
    parcel = next(p for p in world.parcels if p.id == parcel_id)
    for holder, part in split_income(parcel, bonus):
        world = add_coins(world, holder, part)
        world = record_transfer(world, SYSTEM, holder, part, "garden_bonus")
    return world


def pay_reward(world: World, owner_id: str) -> World:
    """Pay the owner of a parcel the reward for a garden that has just been finished on it."""
    world = add_coins(world, owner_id, GARDEN_REWARD)
    return record_transfer(world, SYSTEM, owner_id, GARDEN_REWARD, "garden_reward")
