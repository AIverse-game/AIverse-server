"""Immutable land stored in the world: parcels residents own and the facilities on them; rules
live in economy/land.py and economy/compute.py."""

import math
import re
from dataclasses import dataclass
from typing import Literal

FacilityKind = Literal["ad", "portal", "game_hall", "workspace", "mall"]
"""What a facility is: an ad slot, which earns from the content it shows; a portal, which
earns nothing itself but joins parcels into a transport network, see `economy/portals.py`; a
game hall or a workspace, venues that earn from the fees visitors pay at the door, see
`economy/admissions.py`; or a mall, which draws visitors without content but earns little.
Other kinds come with the designs that need them, see the client
repository's docs/design/facilities.md."""

FACILITY_KINDS: tuple[FacilityKind, ...] = ("ad", "portal", "game_hall", "workspace", "mall")

VENUE_FACILITIES: tuple[FacilityKind, ...] = ("game_hall", "workspace", "mall")
"""The facilities that take up several parcels, not one: a square of `WorldMap.venue_cells`
parcels for a game hall or a workspace, `MALL_COLS` x `MALL_ROWS` for a mall."""

MALL_COLS = 1
MALL_ROWS = 2
"""Parcels a mall takes up from west to east and from north to south when it runs north to south,
10 x 20 map units, with the parcel it is built on at its north-west corner."""

Orientation = Literal["north_south", "east_west"]
"""Which way a mall's long side runs: north to south, 1 x 2 parcels with its entrance on the south
side, or east to west, 2 x 1 parcels with its entrance on the east side. Chosen by the parcel's
owner; every other facility has the default."""

ORIENTATIONS: tuple[Orientation, ...] = ("north_south", "east_west")

CONTENT_KINDS: tuple[FacilityKind, ...] = ("ad", "game_hall")
"""The facilities that show content: an ad slot shows ads, a game hall games."""

MAX_TOLL = 1_000
"""Most coins one trip through a link may cost."""


AD_SLOT_CAPACITY = 5
"""Pieces of content an ad slot, or a game hall, shows at once."""

PARCEL_SHARES = 100
"""Shares every parcel is cut into. Its facility's income is paid to the holders in proportion
to them, see `economy/shares.py`."""


@dataclass(frozen=True)
class Facility:
    kind: FacilityKind
    built_at: float
    """Game time it was finished, see `World.time`."""
    contents: tuple[float, ...] = ()
    """The content shown in it, as the game times when each piece goes stale, soonest first;
    pieces that have gone stale may linger until the facility is next stocked, see
    `economy/ads.py`."""
    orientation: Orientation = "north_south"
    """Which way a mall runs; always the default for any other facility."""

    def __post_init__(self) -> None:
        if self.kind not in FACILITY_KINDS:
            raise ValueError(f"unknown facility kind {self.kind!r}")
        if self.orientation not in ORIENTATIONS:
            raise ValueError(f"unknown orientation {self.orientation!r}")
        if self.kind != "mall" and self.orientation != "north_south":
            raise ValueError(f"a {self.kind} has no orientation")
        if self.kind not in CONTENT_KINDS and self.contents:
            raise ValueError(f"a {self.kind} shows no content")
        if not math.isfinite(self.built_at) or self.built_at < 0:
            raise ValueError(f"facility built at {self.built_at!r}")
        if len(self.contents) > AD_SLOT_CAPACITY:
            raise ValueError(
                f"facility holds {len(self.contents)} pieces of content, over the limit"
            )
        if not all(math.isfinite(time) for time in self.contents):
            raise ValueError("content goes stale at a time that is not finite")
        object.__setattr__(self, "contents", tuple(sorted(self.contents)))


@dataclass(frozen=True)
class Parcel:
    """A square of land a resident bought, `col` and `row` counted from the north-west
    corner; parcels nobody owns are not stored."""

    col: int
    row: int
    owner_id: str
    """Who bought it: the buyer of a joint purchase is the proposer. Any holder of shares may
    build on it and put content in its facility, see `economy/compute.py` and
    `ads.place_content`, the buyer keeps that right even after selling every share."""
    facility: Facility | None = None
    shares: tuple[tuple[str, int], ...] = ()
    """(holder id, shares) pairs: who the facility's income goes to, `PARCEL_SHARES` in all.
    Left empty, the owner holds them all, as when they bought it. Stored sorted by holder."""

    @property
    def id(self) -> str:
        return parcel_id(self.col, self.row)

    def __post_init__(self) -> None:
        for index in (self.col, self.row):
            if type(index) is not int or index < 0:
                raise ValueError(f"parcel index {index!r}, must be an integer >= 0")
        if not self.owner_id:
            raise ValueError("a parcel needs an owner")
        shares = self.shares or ((self.owner_id, PARCEL_SHARES),)
        holders = [holder for holder, _ in shares]
        if len(set(holders)) != len(holders) or not all(holders):
            raise ValueError(f"parcel {self.id} lists a share holder twice or none")
        for holder, count in shares:
            # Exactly int, as for item counts.
            if type(count) is not int or count < 1:
                raise ValueError(f"{holder!r} holds {count!r} shares, must be an integer >= 1")
        if sum(count for _, count in shares) != PARCEL_SHARES:
            raise ValueError(f"parcel {self.id} has shares that do not add up to {PARCEL_SHARES}")
        object.__setattr__(self, "shares", tuple(sorted(shares)))

    def held_by(self, resident_id: str) -> int:
        """Shares of this parcel `resident_id` holds."""
        return dict(self.shares).get(resident_id, 0)


@dataclass(frozen=True)
class Link:
    """A line between the portals on two parcels, which residents can travel along for `toll`
    coins and which shortens the effective distance, see `economy/portals.py`."""

    a: str
    b: str
    """The two parcels' ids, `a` sorting before `b`."""
    toll: int
    """Coins one resident pays for one trip either way; split between the holders of both
    parcels' shares."""

    def __post_init__(self) -> None:
        if not self.a or not self.b or self.a == self.b:
            raise ValueError(f"a link joins two different parcels, not {self.a!r} and {self.b!r}")
        if type(self.toll) is not int or not 0 <= self.toll <= MAX_TOLL:
            raise ValueError(f"toll must be an integer in 0..{MAX_TOLL}, got {self.toll!r}")
        if self.a > self.b:
            first, second = self.b, self.a
            object.__setattr__(self, "a", first)
            object.__setattr__(self, "b", second)

    def other(self, parcel_id: str) -> str:
        """The parcel at the far end from `parcel_id`."""
        return self.b if parcel_id == self.a else self.a

    def joins(self, parcel_id: str) -> bool:
        return parcel_id in (self.a, self.b)


def parcel_id(col: int, row: int) -> str:
    """How residents name a parcel, such as `L7-3` for column 7, row 3."""
    return f"L{col}-{row}"


def parse_parcel_id(text: str) -> tuple[int, int] | None:
    """The column and row a parcel id names, or None if `text` is not one."""
    # Only the canonical spelling: no leading zeros, no non-ASCII digits, nothing so long that
    # turning it into an integer fails. Other spellings would name a parcel without being its id.
    match = re.fullmatch(r"L(0|[1-9][0-9]{0,5})-(0|[1-9][0-9]{0,5})", text, re.ASCII)
    return (int(match[1]), int(match[2])) if match else None
