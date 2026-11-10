"""The coin ledger: every time coins really change hands, who paid whom, how much and why.

Only real changes of hands are recorded. Coins set aside and handed back, as offers,
contracts do, stay their owner's and are not; the payment made from them
when a deal settles is. Coins coming from or going to outside the town, such as visitors paying
a facility or the price of land, have `SYSTEM` on that side.

The ledger exists to measure the economy, see `game/economy_report.py`: how much of the
money residents make comes from each other rather than from the system. It keeps at most
`LEDGER_LIMIT` transfers and counts the ones it dropped, so a report can tell it is partial.

Like the rest of the world, `record_transfer` returns a new world and leaves its argument
untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .world import World

SYSTEM = "system"
"""The payer or payee standing for everything outside the town."""

LEDGER_LIMIT = 20_000
"""Most transfers the ledger keeps; older ones are dropped first. A 40-minute game with
eight residents makes several thousand."""

Channel = Literal[
    "compute",
    "offer",
    "contract_payment",
    "contract_forfeit",
    "information",
    "exchange",
    "land",
    "sponsors",
    "visitors",
    "shares",
    "toll",
    "commission",
    "garden_reward",
    "garden_bonus",
]
"""What the coins paid for: compute bought from the system, an accepted trade offer, a
delivery under a contract, a supplier's deposit forfeited to the buyer, information sold, a
fill on the exchange, a parcel of land bought from the town, sponsors paying an ad slot's
holders for the views it got, visitors paying at the door of a workspace or game hall, shares
of a parcel sold, a trip along a portal link paid to the holders of both ends, or a fee an owner
paid the resident who built a facility on their parcel, the one-time reward the town pays
the owner of a parcel for a finished garden, or the tenth the town adds to a parcel's income
when its block has a garden, see `economy/gardens.py`."""


@dataclass(frozen=True)
class Transfer:
    time: float
    """Game time of the transfer, see `World.time`."""
    payer: str
    payee: str
    """Resident ids, or `SYSTEM`."""
    amount: int
    channel: Channel
    item: str | None = None
    """The item kind the coins bought, when they bought items."""
    quantity: int = 0
    """How many of `item`; 0 when there is no item."""

    def __post_init__(self) -> None:
        if type(self.amount) is not int or self.amount < 1:
            raise ValueError(f"a transfer moves at least one coin, got {self.amount!r}")
        if self.payer == self.payee:
            raise ValueError(f"{self.payer!r} cannot pay themselves")
        if (self.item is None) != (self.quantity == 0) or self.quantity < 0:
            raise ValueError("a transfer names an item exactly when it has a positive quantity")


@dataclass(frozen=True)
class Ledger:
    transfers: tuple[Transfer, ...] = ()
    """Oldest first."""
    dropped: int = 0
    """Older transfers no longer kept, over `LEDGER_LIMIT`."""

    def __post_init__(self) -> None:
        if len(self.transfers) > LEDGER_LIMIT:
            raise ValueError(f"ledger holds {len(self.transfers)} transfers, over the limit")
        if type(self.dropped) is not int or self.dropped < 0:
            raise ValueError(f"dropped must be an integer >= 0, got {self.dropped!r}")


def record_transfer(
    world: World,
    payer: str,
    payee: str,
    amount: int,
    channel: Channel,
    item: str | None = None,
    quantity: int = 0,
) -> World:
    """Note that `payer` paid `payee` `amount` coins for `channel` at the current game time.
    Only notes it: moving the coins is the caller's job."""
    transfer = Transfer(world.time, payer, payee, amount, channel, item, quantity)
    kept = (*world.ledger.transfers, transfer)
    over = max(len(kept) - LEDGER_LIMIT, 0)
    return replace(world, ledger=Ledger(kept[over:], world.ledger.dropped + over))
