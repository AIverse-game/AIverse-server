"""What the facility on a parcel was paid at its latest payday, for pages to show.

Sponsors pay ad slots and admission fees pay venues, both at the paydays of `sponsors.py`, and
each book keeps what every parcel was paid at the payday it last held (`last_paid`). A parcel is
only in a book's list while that payday paid it, so a page cannot tell from the amount alone
whether a payout is new; `Payout.at`, the game time of the payday, says so: a later `at` is a new
payout. When both books paid the parcel at the same payday the amounts are added.

Like the rest of the world, every function leaves its argument untouched.
"""

from dataclasses import dataclass

from ..core.world import World


@dataclass(frozen=True)
class Payout:
    at: float
    """Game time of the payday that paid it, a multiple of the pay period."""
    coins: int
    """Whole coins paid to the parcel at that payday, before they were split among its holders."""


def latest_payout(world: World, parcel_id: str) -> Payout | None:
    """The most recent payday that paid the parcel's facility, or None when none has, or when the
    ones that did have been followed by paydays of that book that paid it nothing."""
    paid = [
        (at, coins)
        for at, coins in (
            (world.sponsors.topped_up_at, world.sponsors.last_paid_to(parcel_id)),
            (world.admissions.paid_at, world.admissions.last_paid_to(parcel_id)),
        )
        if coins
    ]
    if not paid:
        return None
    latest = max(at for at, _ in paid)
    return Payout(latest, sum(coins for at, coins in paid if at == latest))
