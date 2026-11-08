"""Immutable state of the sponsors who pay for views of ad slots; the rules live in
economy/sponsors.py."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Sponsor:
    """An outside buyer who pays for the views ad slots get. Like the nodes' vendors it is the
    system playing a party from outside the game."""

    id: int
    name: str
    price: float
    """Coins it pays for one person seeing an ad slot."""
    rate: float
    """Coins of budget it gets for each game second, the most it can spend on average."""
    funds: float = 0.0
    """Budget it has now, unspent; never more than the cap in `economy/sponsors.py`."""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a sponsor needs a name")
        if not (math.isfinite(self.price) and self.price > 0):
            raise ValueError(f"sponsor {self.id} pays {self.price!r} a view, must be > 0")
        if not (math.isfinite(self.rate) and self.rate > 0):
            raise ValueError(f"sponsor {self.id} gets {self.rate!r} a second, must be > 0")
        if not (math.isfinite(self.funds) and self.funds >= 0):
            raise ValueError(f"sponsor {self.id} has funds {self.funds!r}, must be >= 0")


@dataclass(frozen=True)
class SponsorBook:
    sponsors: tuple[Sponsor, ...] = ()
    """In order of id."""
    topped_up_at: float = 0.0
    """Game time the sponsors' budgets were last topped up, at the last payday."""
    settled: tuple[tuple[str, float], ...] = ()
    """(parcel id, people) pairs sorted by parcel: how many of the people who have passed the
    slot on a parcel, `Crowd.views`, have been paid for or passed over so far."""
    last_paid: tuple[tuple[str, int], ...] = ()
    """(parcel id, coins) pairs sorted by parcel: what each slot was paid at the latest payday
    that had views to pay for; slots not in it earned nothing then."""

    def __post_init__(self) -> None:
        ids = [sponsor.id for sponsor in self.sponsors]
        if ids != sorted(set(ids)):
            raise ValueError("sponsors must have unique ids, in order")
        if not (math.isfinite(self.topped_up_at) and self.topped_up_at >= 0):
            raise ValueError(f"sponsors topped up at {self.topped_up_at!r}")
        for pairs in (self.settled, self.last_paid):
            parcels = [parcel for parcel, _ in pairs]
            if parcels != sorted(set(parcels)):
                raise ValueError("a parcel is listed once, in order")
        if not all(math.isfinite(people) and people > 0 for _, people in self.settled):
            raise ValueError("settled views must be positive and finite")
        if not all(type(coins) is int and coins > 0 for _, coins in self.last_paid):
            raise ValueError("last paid amounts must be integers > 0")

    def settled_of(self, parcel_id: str) -> float:
        """People who have passed the slot on `parcel_id` and been dealt with."""
        return dict(self.settled).get(parcel_id, 0.0)

    def last_paid_to(self, parcel_id: str) -> int:
        """Coins the slot on `parcel_id` was paid at the latest payday."""
        return dict(self.last_paid).get(parcel_id, 0)
