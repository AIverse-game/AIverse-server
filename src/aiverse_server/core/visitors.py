"""Immutable state of the scripted visitors who walk the map; the rules that move them live in
economy/visitors.py."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .world import Position

Phase = Literal["away", "walking", "staying"]
"""Off the map until `until`; walking to `target`, arriving at `until`; or standing at `target`
until `until`."""

Goal = Literal["place", "wander", "exit"]
"""What a walk is for: to go to the facility on the parcel `Visitor.stop`, an ad slot to see, a
workspace to work in or a game hall to play in; to roam to a random spot because nothing was
worth seeing; or to leave through the entrance."""

RouteStop = Literal["workspace", "game_hall"]
"""A kind of facility on a rough route: the working route goes to a workspace and then a game
hall."""


@dataclass(frozen=True)
class Visitor:
    """One scripted tourist, who stands for many people, see `economy/visitors.py`. Like a
    resident's walk, a walk is only a start, a target and the times of leaving and arriving; where
    the visitor is in between is worked out from them."""

    id: int
    phase: Phase
    until: float
    """Game time the visitor's present phase ends and the next step is taken."""
    at: Position
    """Where it stands, or set off from; the entrance while away."""
    target: Position
    """Where it walks to or stands; `at` while away."""
    departs_at: float = 0.0
    """Game time the present walk began."""
    via: tuple[Position, Position] | None = None
    """The portal the walk goes in at, and the one it comes out of at once, when that is the
    quicker way; the walk is then `at` to the first, `target` from the second."""
    goal: Goal = "exit"
    stop: str | None = None
    """The parcel of the facility the visitor is on its way to or standing at; None unless `goal`
    is `place`."""
    route: tuple[RouteStop, ...] = ()
    """The rough route the visitor follows on this trip, if it has one: the kinds of facility it
    still means to go to, in order; the one it is on its way to or at is not in it. A visitor
    without a route is an ordinary tourist who looks at ad slots; one with a route differs in
    nothing else."""
    trip: int = 0
    """Visits to town so far, counting this one; with `step` it gives the dice of each choice
    their own seed."""
    step: int = 0
    """Choices made on this trip."""
    stops_left: int = 0
    """Ad slots still to see on this trip before leaving."""
    seen: tuple[tuple[str, float], ...] = ()
    """(parcel id, game time) pairs: slots this visitor has been counted at, and until when it
    does not count again."""

    def __post_init__(self) -> None:
        if self.phase not in ("away", "walking", "staying"):
            raise ValueError(f"unknown visitor phase {self.phase!r}")
        if self.goal not in ("place", "wander", "exit"):
            raise ValueError(f"unknown visitor goal {self.goal!r}")
        if (self.goal == "place") != (self.stop is not None):
            raise ValueError("a visitor has a stop exactly when it is going to a place")
        times = (self.until, self.departs_at, *(until for _, until in self.seen))
        if not all(math.isfinite(time) for time in times):
            raise ValueError(f"visitor {self.id} has a time that is not finite")
        for spot in (self.at, self.target, *(self.via or ())):
            if not (math.isfinite(spot.x) and math.isfinite(spot.y)):
                raise ValueError(f"visitor {self.id} is at a spot that is not finite")
        if min(self.trip, self.step, self.stops_left) < 0:
            raise ValueError(f"visitor {self.id} has a negative count")


@dataclass(frozen=True)
class Crowd:
    """All the scripted visitors, and how many people have seen each ad slot."""

    seed: int = 0
    """Seeds the dice the visitors throw, so a game can be played again with the same crowd."""
    visitors: tuple[Visitor, ...] = ()
    """In order of id."""
    views: tuple[tuple[str, float], ...] = ()
    """(parcel id, people) pairs: people who have seen the slot on that parcel so far, sorted
    by parcel. Fractions, since a visitor stands for more than one person."""

    def __post_init__(self) -> None:
        ids = [visitor.id for visitor in self.visitors]
        if ids != sorted(set(ids)):
            raise ValueError("visitors must have unique ids, in order")
        parcels = [parcel for parcel, _ in self.views]
        if parcels != sorted(set(parcels)):
            raise ValueError("views must list each parcel once, sorted")
        if not all(math.isfinite(people) and people > 0 for _, people in self.views):
            raise ValueError("views must be positive and finite")

    def viewed(self, parcel_id: str) -> float:
        """People who have seen the slot on `parcel_id` so far."""
        return dict(self.views).get(parcel_id, 0.0)
