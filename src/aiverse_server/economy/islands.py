"""Islands and the bridges between them: where the scripted visitors may walk.

When the land is sold in blocks of more than one parcel, the web client draws each block as a
floating island, `ISLAND_INSET` map units inside the block on every side, and joins each island
to its neighbours to the east and south by a bridge across the gap, at the middle of the sides
that face. The client works the same out in `packages/protocol/src/mapGrid.ts` and
`apps/web/src/render/islandModel.ts`; keep them in step. The gaps and bridges take no map units
and cost residents nothing: a resident walks straight across the air, as the client shows.

Visitors do not. Within an island a visitor walks straight, and to get to another island it must
cross bridges, island by island, or go through a network of portals, see `portals.Router`.
`walk_from` gives the shortest such walk from a spot, as a distance and as the straight stretches
to walk. On a map whose blocks are single parcels there are no islands and every walk is
straight.
"""

import functools
import heapq
import math

from ..core.world import Position, WorldMap
from .land import PARCEL_SIZE

ISLAND_INSET = 2.0
"""How far each island's edge lies inside its block; the gap between two islands is twice this."""

Leg = tuple[Position, Position]
"""A straight stretch of a walk, from where it starts to where it ends."""


class Walkways:
    """The islands of one map and the bridges between them, worked out once."""

    def __init__(self, world_map: WorldMap) -> None:
        count = max(int(world_map.size // PARCEL_SIZE), 1)
        cells = world_map.block_cells
        self._count = count
        self._cells = cells
        self._side = -(-count // cells)

        def span(first: int) -> tuple[float, float]:
            last = min(first + cells, count) - 1
            low = first * PARCEL_SIZE
            high = world_map.size if last == count - 1 else (last + 1) * PARCEL_SIZE
            return low + ISLAND_INSET, high - ISLAND_INSET

        spans = [span(block * cells) for block in range(self._side)]
        self._rects = [(x0, y0, x1, y1) for y0, y1 in spans for x0, x1 in spans]
        # Each bridge has an end on each of its islands, which are nodes 2k and 2k + 1.
        self._spots: list[Position] = []
        self._island: list[int] = []
        self._ports: list[list[int]] = [[] for _ in self._rects]
        self._graph: list[list[tuple[int, float]]] = []
        for row in range(self._side):
            for col in range(self._side):
                here = row * self._side + col
                if col + 1 < self._side:
                    self._bridge(here, here + 1, east=True)
                if row + 1 < self._side:
                    self._bridge(here, here + self._side, east=False)
        for ports in self._ports:
            for first in ports:
                for second in ports:
                    if first != second:
                        gap = _gap(self._spots[first], self._spots[second])
                        self._graph[first].append((second, gap))
        self._rows: dict[int, tuple[list[float], list[int]]] = {}

    def _bridge(self, a: int, b: int, east: bool) -> None:
        ax0, ay0, ax1, ay1 = self._rects[a]
        bx0, by0, bx1, by1 = self._rects[b]
        if east:
            y = (max(ay0, by0) + min(ay1, by1)) / 2
            ends = (Position(ax1, y), Position(bx0, y))
        else:
            x = (max(ax0, bx0) + min(ax1, bx1)) / 2
            ends = (Position(x, ay1), Position(x, by0))
        first = len(self._spots)
        for island, spot in zip((a, b), ends, strict=True):
            self._ports[island].append(len(self._spots))
            self._island.append(island)
            self._spots.append(spot)
            self._graph.append([])
        across = _gap(*ends)
        self._graph[first].append((first + 1, across))
        self._graph[first + 1].append((first, across))

    def island_of(self, spot: Position) -> int:
        """The island of the block `spot` lies in; a spot in a gap belongs to the block it is in."""
        col = min(max(int(spot.x // PARCEL_SIZE), 0), self._count - 1) // self._cells
        row = min(max(int(spot.y // PARCEL_SIZE), 0), self._count - 1) // self._cells
        return row * self._side + col

    def clamp(self, home: Position, spot: Position) -> Position:
        """`spot` moved onto the island `home` stands on, if it lies off it."""
        x0, y0, x1, y1 = self._rects[self.island_of(home)]
        return Position(min(max(spot.x, x0), x1), min(max(spot.y, y0), y1))

    def row(self, node: int) -> tuple[list[float], list[int]]:
        """Walking distance from a bridge end to every other, and the end before each on the way."""
        known = self._rows.get(node)
        if known is not None:
            return known
        far = [math.inf] * len(self._spots)
        before = [-1] * len(self._spots)
        far[node] = 0.0
        queue = [(0.0, node)]
        while queue:
            reached, here = heapq.heappop(queue)
            if reached > far[here]:
                continue
            for there, step in self._graph[here]:
                if reached + step < far[there]:
                    far[there] = reached + step
                    before[there] = here
                    heapq.heappush(queue, (far[there], there))
        self._rows[node] = (far, before)
        return far, before

    def bridges(self) -> tuple[Leg, ...]:
        """Every bridge as the two spots it joins, in the order the client lists them: for each
        island row by row, the one east of it, then the one south."""
        return tuple(
            (self._spots[node], self._spots[node + 1]) for node in range(0, len(self._spots), 2)
        )

    def ports(self, island: int) -> list[int]:
        return self._ports[island]

    def spot(self, node: int) -> Position:
        return self._spots[node]


class WalkFrom:
    """The shortest walks from one spot to any other: straight where there are no islands or
    both spots are on one, otherwise across the bridges."""

    def __init__(self, ways: Walkways | None, origin: Position) -> None:
        self._ways = ways
        self._origin = origin
        self._reach: dict[int, tuple[float, int]] = {}
        if ways is not None:
            for port in ways.ports(ways.island_of(origin)):
                first = _gap(origin, ways.spot(port))
                far, _ = ways.row(port)
                for node, onward in enumerate(far):
                    if first + onward < self._reach.get(node, (math.inf, 0))[0]:
                        self._reach[node] = (first + onward, port)

    def distance(self, end: Position) -> float:
        """Map units to walk to `end`."""
        ways = self._ways
        if ways is None or ways.island_of(end) == ways.island_of(self._origin):
            return _gap(self._origin, end)
        return min(
            (
                self._reach[node][0] + _gap(ways.spot(node), end)
                for node in ways.ports(ways.island_of(end))
            ),
            default=math.inf,
        )

    def legs(self, end: Position) -> tuple[Leg, ...]:
        """The straight stretches of the shortest walk to `end`, in order; one when it is
        straight."""
        ways = self._ways
        if ways is None or ways.island_of(end) == ways.island_of(self._origin):
            return ((self._origin, end),)
        last = min(
            ways.ports(ways.island_of(end)),
            key=lambda node: self._reach[node][0] + _gap(ways.spot(node), end),
        )
        port = self._reach[last][1]
        _, before = ways.row(port)
        nodes = [last]
        while nodes[-1] != port:
            nodes.append(before[nodes[-1]])
        nodes.reverse()
        spots = [self._origin, *(ways.spot(node) for node in nodes), end]
        return tuple(zip(spots, spots[1:], strict=False))


def _gap(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))


@functools.lru_cache(maxsize=8)
def walkways(world_map: WorldMap) -> Walkways | None:
    """The islands and bridges of `world_map`, or None when its blocks are single parcels and
    so there are none."""
    return Walkways(world_map) if world_map.block_cells > 1 else None


@functools.lru_cache(maxsize=1024)
def walk_from(world_map: WorldMap, origin: Position) -> WalkFrom:
    """The shortest walks from `origin` on `world_map`; kept for the spots asked about often, such
    as the portals."""
    return WalkFrom(walkways(world_map), origin)


def on_island(world_map: WorldMap, home: Position, spot: Position) -> Position:
    """`spot` moved onto the island `home` is on; unchanged where there are no islands."""
    ways = walkways(world_map)
    return spot if ways is None else ways.clamp(home, spot)


def bridges(world_map: WorldMap) -> tuple[Leg, ...]:
    """Every bridge of `world_map`, see `Walkways.bridges`; none where there are no islands."""
    ways = walkways(world_map)
    return () if ways is None else ways.bridges()
