"""Immutable state of the admission fees visitors pay workspaces and game halls; the rules live
in economy/admissions.py."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AdmissionBook:
    unpaid: tuple[tuple[str, float], ...] = ()
    """(parcel id, coins) pairs sorted by parcel: fees visitors have paid at the door and the
    holders have not been paid yet; paid out in whole coins at paydays, the rest carried."""
    admitted: tuple[tuple[str, float], ...] = ()
    """(parcel id, people) pairs sorted by parcel: people let in so far. A visitor stands for
    more than one person, so these are fractions."""
    paid_at: float = 0.0
    """The last payday, a multiple of the pay period, at which fees were paid out."""
    last_paid: tuple[tuple[str, int], ...] = ()
    """(parcel id, coins) pairs sorted by parcel: what each facility was paid at the latest
    payday that paid anything; facilities not in it earned nothing then."""

    def __post_init__(self) -> None:
        for pairs in (self.unpaid, self.admitted, self.last_paid):
            parcels = [parcel for parcel, _ in pairs]
            if parcels != sorted(set(parcels)):
                raise ValueError("a parcel is listed once, in order")
        for _, amount in (*self.unpaid, *self.admitted):
            if not (math.isfinite(amount) and amount > 0):
                raise ValueError("admission amounts must be positive and finite")
        if not all(type(coins) is int and coins > 0 for _, coins in self.last_paid):
            raise ValueError("last paid amounts must be integers > 0")
        if not (math.isfinite(self.paid_at) and self.paid_at >= 0):
            raise ValueError(f"admissions paid at {self.paid_at!r}")

    def admitted_to(self, parcel_id: str) -> float:
        """People let into the facility on `parcel_id` so far."""
        return dict(self.admitted).get(parcel_id, 0.0)

    def last_paid_to(self, parcel_id: str) -> int:
        """Coins the facility on `parcel_id` was paid at the latest payday."""
        return dict(self.last_paid).get(parcel_id, 0)
