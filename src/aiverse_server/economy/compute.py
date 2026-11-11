"""Compute: how much work a resident can have done at a time, and what building costs.

Compute is a speed, not something carried: a piece of work has a workload, and the time it takes
is its workload over the rate of the compute doing it. Work runs on its source, so the resident
need not stay by it; they can walk, talk and trade while it runs, and it finishes without them
as long as they are still in the game. There are three sources:

- `self`: every resident's own compute, free but slow, `SELF_RATE`; its jobs run one after
  another, a new one queueing behind the rest, up to `SELF_QUEUE_LIMIT` waiting;
- `on_demand`: a system node hired for one job, `NODE_RATE`, paid for by the workload at
  `ON_DEMAND_PRICE`;
- `subscription`: a system node the resident subscribed to for a stretch of time with
  `buy_node`, theirs for that time and doing one job at a time at `NODE_RATE`, paid for by the
  second at `SUBSCRIPTION_PRICE` whether it is busy or not. Having several nodes runs several
  jobs at once. A job still running when the subscription runs out carries on at `SELF_RATE`,
  the speed of one's own compute, without using up the resident's own one-job-at-a-time slot.

Nodes have a kind, and a job needs a node of its kind: generative nodes build facilities,
render nodes make content. There are eight jobs. `build_ad` builds an ad slot, `build_portal`
a portal, see `portals.py`, `build_garden` a garden, see `gardens.py`, and `build_game_hall`,
`build_workspace` and `build_mall` the venues, on a parcel the resident owns, wherever the
resident is. `make_content` renders one piece of content for an ad slot and `make_game` one for a
game hall, each an item the resident then carries, see `ads.py`. Several content jobs may run at
once, each on a source of its own.

Compute is bought from the system, so its price leaves the game, and nothing is paid back for
unused subscriptions, running jobs or facilities when a resident leaves or the game ends.

The town has only so many nodes of each kind, `node_supply`, the same for both kinds. A
subscribed node takes one until it runs out, and a hired one until its job ends; with none free
of a kind, nobody can subscribe to or hire one of that kind until one comes back. A job carrying
on at the resident's own speed after its node ran out takes none.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.compute import (
    MAKING_JOBS,
    NODE_KINDS,
    SELF_JOBS,
    VENUE_JOB_FACILITY,
    ComputeBook,
    Job,
    JobKind,
    JobSource,
    Node,
    NodeKind,
    Permit,
    PermitKind,
)
from ..core.inventory import add_coins, store_item, take_coins
from ..core.land import (
    ORIENTATIONS,
    VENUE_FACILITIES,
    Facility,
    FacilityKind,
    Orientation,
    Parcel,
)
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import World
from .ads import CONTENT_ITEM, GAME_ITEM
from .gardens import garden_planned, pay_reward
from .land import claimed, footprint_size, venue_fits, venue_footprint
from .portals import MAX_PORTALS, portal_count

SELF_RATE = 0.5
"""Workload a resident's own compute does per game second."""

SELF_QUEUE_LIMIT = SELF_JOBS - 1
"""Own-compute jobs that may wait behind the one running."""

NODE_RATE = 10.0
"""Workload a node does per game second."""

AD_WORKLOAD = 180.0
"""Workload of building an ad slot: six minutes of one's own compute at `SELF_RATE`, 18 seconds of
a node."""

CONTENT_WORKLOAD = 60.0
"""Workload of rendering a piece of content: two minutes of one's own compute, 6 seconds of a
node."""

PORTAL_WORKLOAD = 180.0
"""Workload of building a portal: six minutes of one's own compute, 18 seconds of a node.
Dearer than an ad slot, which earns by itself."""

VENUE_WORKLOAD = 240.0
"""Workload of building a game hall or a workspace: eight minutes of one's own compute, 24 seconds
of a node. Dearest of the facilities, since a venue is worth more once visitors come."""

MALL_WORKLOAD = 200.0
"""Workload of building a mall: some seven minutes of one's own compute, 20 seconds of a node.
A little cheaper than a game hall or a workspace, since it earns far less."""

GARDEN_WORKLOAD = 100.0
"""Workload of building a garden: some three minutes of one's own compute, 10 seconds of a node.
The cheapest facility to build, and half what a game hall costs."""

GAME_WORKLOAD = 90.0
"""Workload of rendering a game for a game hall: three minutes of one's own compute, 9 seconds of
a node. Dearer than an ad."""

ContentKind = Literal["ad", "game"]
"""What a content job renders: an ad for an ad slot or a game for a game hall."""

VenueKind = Literal["game_hall", "workspace", "mall"]
"""The facilities `start_build_venue` builds."""

NODE_FOR_JOB: dict[JobKind, NodeKind] = {
    "build_ad": "generative",
    "build_portal": "generative",
    "build_game_hall": "generative",
    "build_workspace": "generative",
    "build_mall": "generative",
    "build_garden": "generative",
    "make_content": "render",
    "make_game": "render",
}
"""The kind of node a job needs."""

WORKLOADS: dict[JobKind, float] = {
    "build_ad": AD_WORKLOAD,
    "build_portal": PORTAL_WORKLOAD,
    "build_game_hall": VENUE_WORKLOAD,
    "build_workspace": VENUE_WORKLOAD,
    "build_mall": MALL_WORKLOAD,
    "build_garden": GARDEN_WORKLOAD,
    "make_content": CONTENT_WORKLOAD,
    "make_game": GAME_WORKLOAD,
}

BUILD_JOB: dict[VenueKind, JobKind] = {
    "game_hall": "build_game_hall",
    "workspace": "build_workspace",
    "mall": "build_mall",
}
MAKE_JOB: dict[ContentKind, JobKind] = {"ad": "make_content", "game": "make_game"}
BUILT_BY: dict[JobKind, FacilityKind] = {
    "build_ad": "ad",
    "build_portal": "portal",
    "build_game_hall": "game_hall",
    "build_workspace": "workspace",
    "build_mall": "mall",
    "build_garden": "garden",
}
"""The facility each building job leaves."""

ON_DEMAND_PRICE = 15
"""Coins per unit of workload of a job on a node hired for it alone. Deliberately dear: an ad slot
costs 2700 coins hired, a venue 3600, a game 1350, so hiring is a rare last resort."""

SUBSCRIPTION_PRICE = 12
"""Coins per second of a subscribed node, busy or idle. A node does 10 workload a second, so
subscribed work costs 1.2 coins per workload against 15 hired: an ad slot 216 coins, a game 108,
and the same node keeps going for the whole subscription. A subscription costs more in all than
one hired job, but is cheaper per job once the node is kept busy more than about a twelfth of
the time."""

NODE_SECONDS = (300, 1200)
"""Least and most game seconds one subscription lasts. Subscriptions are meant to be long: a node
is a standing workshop that is kept busy, not something to switch on for a single job."""

PERMIT_SECONDS = 300.0
"""How long a permit to build on another resident's parcel waits for the builder to start the
job before it lapses and the owner's fee goes back."""

MAX_NODES = 4
"""Subscribed nodes one resident may hold at a time."""

NODE_SUPPLY_SHARE = (2, 5)
"""Nodes of each kind in the town for every resident, as a fraction: two for every five
residents, rounded down, so eight residents share three generative and three render nodes."""

ComputeErrorCode = Literal[
    "unknown_resident",
    "invalid_seconds",
    "too_many_nodes",
    "unknown_node_kind",
    "unknown_venue_kind",
    "unknown_content_kind",
    "not_your_parcel",
    "parcel_built",
    "parcel_covered",
    "no_room_for_venue",
    "invalid_source",
    "source_busy",
    "self_queue_full",
    "unknown_node",
    "wrong_node_kind",
    "node_expired",
    "too_many_portals",
    "unknown_parcel",
    "invalid_commission",
    "invalid_fee",
    "parcel_permitted",
    "too_many_commissions",
    "invalid_orientation",
    "wrong_orientation",
    "garden_exists",
    "no_node_free",
]


class ComputeError(Exception):
    def __init__(self, code: ComputeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ComputeErrorCode = code
        self.message = message


def job_seconds(workload: float, source: JobSource) -> float:
    """Game seconds a job of `workload` takes on `source`."""
    return workload / (SELF_RATE if source == "self" else NODE_RATE)


def subscription_job_end(workload: float, now: float, node_expires_at: float) -> float:
    """Game time a job of `workload` started at `now` on a node running out at `node_expires_at`
    ends: at the node's speed until then, and at the resident's own after that."""
    on_node = node_expires_at - now
    if workload <= on_node * NODE_RATE:
        return now + workload / NODE_RATE
    return node_expires_at + (workload - on_node * NODE_RATE) / SELF_RATE


def on_demand_cost(workload: float) -> int:
    """Coins hiring a node for one job of `workload` costs."""
    return round(workload * ON_DEMAND_PRICE)


def subscription_cost(seconds: int) -> int:
    """Coins a subscription of `seconds` costs."""
    return seconds * SUBSCRIPTION_PRICE


def node_supply(world: World) -> int:
    """Nodes of each kind in the town: `NODE_SUPPLY_SHARE` of the residents who began the game,
    those who have left included, rounded down, and at least one."""
    per, residents = NODE_SUPPLY_SHARE
    return max(1, len(world.residents) * per // residents)


def nodes_in_use(world: World, kind: NodeKind) -> tuple[float, ...]:
    """When each node of `kind` taken now comes back, soonest first: a subscribed node when it
    runs out, a hired one when its job ends."""
    now = world.time
    book = world.compute
    subscribed = [node.expires_at for node in book.nodes if node.kind == kind]
    hired = [
        job.ends_at
        for job in book.jobs
        if job.source == "on_demand" and NODE_FOR_JOB[job.kind] == kind
    ]
    return tuple(sorted(until for until in subscribed + hired if until > now))


def nodes_free(world: World, kind: NodeKind) -> int:
    """Nodes of `kind` nobody has taken now."""
    return max(node_supply(world) - len(nodes_in_use(world, kind)), 0)


def _check_node_free(world: World, kind: NodeKind) -> None:
    if nodes_free(world, kind) <= 0:
        raise ComputeError(
            "no_node_free",
            f"all {node_supply(world)} {kind} nodes of the town are taken; one comes back in "
            f"{nodes_in_use(world, kind)[0] - world.time:.0f}s",
        )


def buy_node(world: World, resident_id: str, kind: NodeKind, seconds: int) -> World:
    """Subscribe to a node of `kind` for `seconds` from now; the coins go to the system. One of
    the town's nodes of that kind must be free."""
    _find(world, resident_id)
    if kind not in NODE_KINDS:
        raise ComputeError("unknown_node_kind", f"no nodes of kind {kind!r}")
    low, high = NODE_SECONDS
    if type(seconds) is not int or not low <= seconds <= high:
        raise ComputeError(
            "invalid_seconds",
            f"a subscription lasts a whole number of seconds from {low} to {high}",
        )
    book = world.compute
    if sum(node.owner_id == resident_id for node in book.nodes) >= MAX_NODES:
        raise ComputeError("too_many_nodes", f"at most {MAX_NODES} nodes may be held at once")
    _check_node_free(world, kind)
    cost = subscription_cost(seconds)
    world = take_coins(world, resident_id, cost)
    world = record_transfer(world, resident_id, SYSTEM, cost, "compute")
    node = Node(book.next_node_id, resident_id, kind, world.time, world.time + seconds)
    book = replace(book, nodes=(*book.nodes, node), next_node_id=book.next_node_id + 1)
    return replace(world, compute=book)


def start_build_ad(
    world: World,
    resident_id: str,
    parcel_id: str,
    source: JobSource,
    node_id: int | None = None,
) -> World:
    """Start building an ad slot on the bare parcel `parcel_id` of the resident, on `source`;
    `node_id` names the node of a subscription and is refused for the other sources. The ad
    slot appears when the job ends. The parcel may be another resident's when its owner gave the
    resident a permit, see `commissions.py`; the owner's fee is then paid when the job ends."""
    _find(world, resident_id)
    owner_id, permit = _owner_or_permit(world, resident_id, parcel_id, "ad")
    parcel = _bare_parcel(world, owner_id, parcel_id)
    return _start_job(world, resident_id, "build_ad", parcel.id, source, node_id, permit)


def start_build_portal(
    world: World,
    resident_id: str,
    parcel_id: str,
    source: JobSource,
    node_id: int | None = None,
) -> World:
    """Start building a portal on the bare parcel `parcel_id` of the resident, as
    `start_build_ad` does for an ad slot: the parcel is theirs when they bought it or hold
    shares in it, and the portal belongs to the parcel, so its owner links it and its holders
    share what it earns. The parcel's owner may have at most `MAX_PORTALS` on their parcels,
    built or being built by anyone, see `portal_count`."""
    _find(world, resident_id)
    parcel = _bare_parcel(world, resident_id, parcel_id)
    if portal_count(world, parcel.owner_id) >= MAX_PORTALS:
        raise ComputeError(
            "too_many_portals", f"at most {MAX_PORTALS} portals may stand on one resident's parcels"
        )
    return _start_job(world, resident_id, "build_portal", parcel.id, source, node_id)


def start_build_garden(
    world: World,
    resident_id: str,
    parcel_id: str,
    source: JobSource,
    node_id: int | None = None,
) -> World:
    """Start building a garden on the bare parcel `parcel_id` of the resident, as
    `start_build_ad` does for an ad slot. Only the parcel's owner builds one, and a block has at
    most one, built or being built, see `gardens.py`. When it is finished the town pays the owner
    `GARDEN_REWARD` coins."""
    _find(world, resident_id)
    parcel = _bare_parcel(world, resident_id, parcel_id)
    if parcel.owner_id != resident_id:
        # The reward and the upkeep of the bonus are the owner's, so only they build one.
        raise ComputeError("not_your_parcel", f"parcel {parcel_id!r} is not {resident_id}'s")
    if garden_planned(world, parcel.id):
        raise ComputeError(
            "garden_exists", f"the block of {parcel_id} already has a garden, or one being built"
        )
    return _start_job(world, resident_id, "build_garden", parcel.id, source, node_id)


def portal_room(world: World, parcel: Parcel, taken: dict[tuple[int, int], str]) -> bool:
    """Whether a portal could be started on `parcel` now by someone who may build there: it is
    bare and untaken, no one holds a permit for it, and its owner has room for another. `taken`
    is `land.claimed(world)`."""
    return (
        parcel.facility is None
        and (parcel.col, parcel.row) not in taken
        and promised_builder(world, parcel.id, None) is None
        and portal_count(world, parcel.owner_id) < MAX_PORTALS
    )


def garden_room(world: World, parcel: Parcel, taken: dict[tuple[int, int], str]) -> bool:
    """Whether the owner of `parcel` could start a garden on it now: it is bare and untaken, no
    one holds a permit for it, and its block has no garden, built or being built. `taken` is
    `land.claimed(world)`."""
    return (
        parcel.facility is None
        and (parcel.col, parcel.row) not in taken
        and promised_builder(world, parcel.id, None) is None
        and not garden_planned(world, parcel.id)
    )


def start_build_venue(
    world: World,
    resident_id: str,
    parcel_id: str,
    kind: VenueKind,
    source: JobSource,
    node_id: int | None = None,
    orientation: Orientation | None = None,
) -> World:
    """Start building a game hall or a workspace, `kind`, on the bare parcel `parcel_id` of the
    resident, as `start_build_ad` does for an ad slot. A venue takes up the square of
    `venue_cells` parcels with that parcel at its north-west corner: all of them must be the
    parcel owner's, untaken, and in the same block. The parcel may be another resident's when
    its owner gave the resident a permit, as for an ad slot. A mall runs `orientation`, north to
    south when not said; on a permit it runs as the owner said, and `orientation` may only repeat
    that."""
    _find(world, resident_id)
    job = BUILD_JOB.get(kind)
    if job is None:
        raise ComputeError("unknown_venue_kind", f"no venues of kind {kind!r}")
    owner_id, permit = _owner_or_permit(world, resident_id, parcel_id, kind)
    runs = _orientation(kind, orientation, permit)
    parcel = _bare_parcel(world, owner_id, parcel_id, venue=kind, orientation=runs)
    return _start_job(world, resident_id, job, parcel.id, source, node_id, permit, runs)


def start_make_content(
    world: World,
    resident_id: str,
    source: JobSource,
    node_id: int | None = None,
    content: ContentKind = "ad",
) -> World:
    """Start rendering a piece of content for the resident on `source`, as `start_build_ad`
    does for an ad slot: an ad, or a game for a game hall. The piece joins what they carry when
    the job ends."""
    _find(world, resident_id)
    job = MAKE_JOB.get(content)
    if job is None:
        raise ComputeError("unknown_content_kind", f"no content of kind {content!r}")
    return _start_job(world, resident_id, job, None, source, node_id)


def _start_job(
    world: World,
    resident_id: str,
    kind: JobKind,
    parcel_id: str | None,
    source: JobSource,
    node_id: int | None,
    permit: Permit | None = None,
    orientation: Orientation = "north_south",
) -> World:
    book = world.compute
    workload = WORKLOADS[kind]
    ends_at = world.time + job_seconds(workload, source)
    cost = 0
    if source == "subscription":
        if type(node_id) is not int:
            raise ComputeError("invalid_source", "a subscription job names its node by number")
        node = next((n for n in book.nodes if n.id == node_id), None)
        if node is None or node.owner_id != resident_id:
            raise ComputeError("unknown_node", f"resident {resident_id!r} has no node {node_id}")
        if node.kind != NODE_FOR_JOB[kind]:
            raise ComputeError(
                "wrong_node_kind",
                f"node {node_id} is {node.kind}; {kind} needs a {NODE_FOR_JOB[kind]} node",
            )
        if any(job.node_id == node_id for job in book.jobs):
            raise ComputeError("source_busy", f"node {node_id} is already at work")
        if node.expires_at <= world.time:
            raise ComputeError("node_expired", f"node {node_id} has run out")
        ends_at = subscription_job_end(workload, world.time, node.expires_at)
    else:
        if node_id is not None:
            raise ComputeError("invalid_source", f"{source!r} compute is not a node")
        if source == "self":
            own = [job for job in book.jobs if job.owner_id == resident_id and job.source == "self"]
            if len(own) >= SELF_JOBS:
                raise ComputeError(
                    "self_queue_full",
                    f"your own compute already has {len(own)} jobs, running or queued",
                )
            if own:
                ends_at = max(job.ends_at for job in own) + job_seconds(workload, "self")
        elif source == "on_demand":
            _check_node_free(world, NODE_FOR_JOB[kind])
            cost = on_demand_cost(workload)
        else:
            raise ComputeError("invalid_source", f"no compute source {source!r}")
    if parcel_id is not None and permit is None:
        promised = promised_builder(world, parcel_id, VENUE_JOB_FACILITY.get(kind), orientation)
        if promised is not None:
            raise ComputeError(
                "parcel_permitted",
                f"parcel {parcel_id} is promised to {promised} until the permit lapses",
            )
    if cost:
        world = take_coins(world, resident_id, cost)
        world = record_transfer(world, resident_id, SYSTEM, cost, "compute")
    job = Job(
        book.next_job_id,
        resident_id,
        kind,
        parcel_id,
        source,
        node_id,
        world.time,
        ends_at,
        permit.fee if permit is not None else 0,
        permit.owner_id if permit is not None else None,
        orientation,
    )
    permits = tuple(p for p in book.permits if p is not permit)
    book = replace(book, jobs=(*book.jobs, job), next_job_id=book.next_job_id + 1, permits=permits)
    return replace(world, compute=book)


def finish_jobs(world: World) -> World:
    """Finish every job due by `world.time`, oldest end first: its facility appears, or its
    piece of content joins what its owner carries. The same world when none is due."""
    due = sorted(
        (job for job in world.compute.jobs if job.ends_at <= world.time),
        key=lambda job: (job.ends_at, job.id),
    )
    for job in due:
        if all(other.id != job.id for other in world.compute.jobs):
            continue  # dropped when a rival on its parcel finished first
        book = replace(world.compute, jobs=tuple(j for j in world.compute.jobs if j.id != job.id))
        world = replace(world, compute=book)
        if job.kind in MAKING_JOBS:
            item = CONTENT_ITEM if job.kind == "make_content" else GAME_ITEM
            world = store_item(world, job.owner_id, item)
            continue
        site = next((parcel for parcel in world.parcels if parcel.id == job.parcel_id), None)
        if site is None or site.facility is not None:
            # Another job on the same parcel finished first; the compute spent is lost, the
            # owner's fee goes back.
            if job.fee and job.fee_from is not None:
                world = add_coins(world, job.fee_from, job.fee)
            continue
        for rival in world.compute.jobs:
            if rival.parcel_id == job.parcel_id:
                world = _drop_rival(world, rival)
        built = Facility(BUILT_BY[job.kind], world.time, orientation=job.orientation)
        parcels = tuple(
            replace(parcel, facility=built) if parcel.id == job.parcel_id else parcel
            for parcel in world.parcels
        )
        world = replace(world, parcels=parcels)
        if job.fee and job.fee_from is not None:
            world = add_coins(world, job.owner_id, job.fee)
            world = record_transfer(world, job.fee_from, job.owner_id, job.fee, "commission")
        if built.kind == "garden":
            world = pay_reward(world, site.owner_id)
    return world


def _drop_rival(world: World, job: Job) -> World:
    """Drop a job on a parcel that another job has just built on; its fee goes back."""
    if job.fee and job.fee_from is not None:
        world = add_coins(world, job.fee_from, job.fee)
    book = world.compute
    return replace(world, compute=replace(book, jobs=tuple(j for j in book.jobs if j.id != job.id)))


def expire_nodes(world: World) -> World:
    """Drop every subscribed node that has run out by `world.time`; jobs due by then finish
    first, and any still running carry on without a node, at the speed of the resident's own
    compute, as `subscription_job_end` worked out when they started. The same world when no
    node has run out."""
    book = world.compute
    nodes = tuple(node for node in book.nodes if node.expires_at > world.time)
    if len(nodes) == len(book.nodes):
        return world
    kept = {node.id for node in nodes}
    jobs = tuple(
        replace(job, node_id=None) if job.node_id is not None and job.node_id not in kept else job
        for job in book.jobs
    )
    return replace(world, compute=replace(book, nodes=nodes, jobs=jobs))


def expire_permits(world: World) -> World:
    """Drop every permit that has lapsed by `world.time`, handing the owner's fee back. The same
    world when none has."""
    book = world.compute
    lapsed = [permit for permit in book.permits if permit.expires_at <= world.time]
    if not lapsed:
        return world
    for permit in lapsed:
        world = add_coins(world, permit.owner_id, permit.fee)
    kept = tuple(permit for permit in book.permits if permit.expires_at > world.time)
    return replace(world, compute=replace(book, permits=kept))


def next_compute_due(world: World) -> float | None:
    """Game time when the earliest job ends, node runs out or permit lapses, or None when there
    is none."""
    book = world.compute
    times = (
        [job.ends_at for job in book.jobs]
        + [node.expires_at for node in book.nodes]
        + [permit.expires_at for permit in book.permits]
    )
    return min(times, default=None)


def permit_coins(world: World, resident_id: str) -> int:
    """Coins the resident has set aside as the owner of parcels others may build on: the fees of
    permits not yet used and of jobs not yet finished."""
    book = world.compute
    return sum(p.fee for p in book.permits if p.owner_id == resident_id) + sum(
        job.fee for job in book.jobs if job.fee_from == resident_id
    )


def cancel_compute(world: World, resident_id: str | None = None) -> World:
    """Drop the resident's jobs, nodes and permits as builder or owner, or everyone's when
    `resident_id` is None. Nothing is paid back for compute and nothing is made, but a fee an
    owner set aside for a dropped job or permit goes back to them."""
    book = world.compute

    def theirs(*ids: str | None) -> bool:
        return resident_id is None or resident_id in ids

    for job in book.jobs:
        if theirs(job.owner_id) and job.fee and job.fee_from is not None:
            world = add_coins(world, job.fee_from, job.fee)
    for permit in book.permits:
        if theirs(permit.owner_id, permit.builder_id):
            world = add_coins(world, permit.owner_id, permit.fee)
    keep = ComputeBook(
        tuple(n for n in book.nodes if not theirs(n.owner_id)),
        tuple(j for j in book.jobs if not theirs(j.owner_id)),
        book.next_node_id,
        book.next_job_id,
        tuple(p for p in book.permits if not theirs(p.owner_id, p.builder_id)),
    )
    return replace(world, compute=keep)


def promised_builder(
    world: World, parcel_id: str, venue: str | None, orientation: Orientation = "north_south"
) -> str | None:
    """The builder holding a permit for a parcel that a facility on `parcel_id` would take up,
    or None when none is promised. `venue` is the kind of venue it would be, a mall running as
    `orientation`, None for a facility of one parcel. A permit for a venue promises every parcel
    the venue would take up, not only the one it names."""

    def cells(anchor_id: str, kind: str | None, runs: Orientation) -> set[tuple[int, int]]:
        anchor = next(p for p in world.parcels if p.id == anchor_id)
        if kind in VENUE_FACILITIES:
            return set(venue_footprint(world.map, anchor.col, anchor.row, kind, runs))
        return {(anchor.col, anchor.row)}

    wanted = cells(parcel_id, venue, orientation)
    for permit in world.compute.permits:
        if wanted & cells(permit.parcel_id, permit.kind, permit.orientation):
            return permit.builder_id
    return None


def check_buildable(
    world: World,
    owner_id: str,
    parcel_id: str,
    kind: PermitKind,
    orientation: Orientation = "north_south",
) -> None:
    """Refuse unless `owner_id` could build a facility of `kind` on `parcel_id` now, a mall
    running as `orientation`: it is theirs, bare, not taken up by a venue, and for a venue all of
    its land fits. Raises `ComputeError`."""
    _bare_parcel(
        world, owner_id, parcel_id, venue=None if kind == "ad" else kind, orientation=orientation
    )
    if promised_builder(world, parcel_id, None if kind == "ad" else kind, orientation) is not None:
        raise ComputeError(
            "parcel_permitted", f"parcel {parcel_id} is already promised to a builder"
        )


def _owner_or_permit(
    world: World, resident_id: str, parcel_id: str, kind: PermitKind
) -> tuple[str, Permit | None]:
    """Whose parcel `parcel_id` is, and the permit that lets `resident_id` build `kind` on it
    when it is not theirs. A permit is used first; else a holder of shares in the parcel builds as
    the owner does, with no permit."""
    parcel = next((p for p in world.parcels if p.id == parcel_id), None)
    if parcel is None:
        raise ComputeError("not_your_parcel", f"parcel {parcel_id!r} is not {resident_id}'s")
    permit = next(
        (
            p
            for p in world.compute.permits
            if p.parcel_id == parcel_id and p.builder_id == resident_id and p.kind == kind
        ),
        None,
    )
    if permit is not None:
        return permit.owner_id, permit
    if _is_theirs(parcel, resident_id):
        return resident_id, None
    raise ComputeError(
        "not_your_parcel",
        f"parcel {parcel_id!r} is not {resident_id}'s, and its owner has not permitted "
        f"{resident_id} to build a {kind.replace('_', ' ')} there",
    )


def _orientation(kind: str, asked: Orientation | None, permit: Permit | None) -> Orientation:
    """Which way a venue of `kind` runs: as the permit's owner said, else as asked, else north to
    south. Only a mall has a choice."""
    if asked is not None and asked not in ORIENTATIONS:
        raise ComputeError("invalid_orientation", f"no orientation {asked!r}")
    if kind != "mall":
        if asked not in (None, "north_south"):
            raise ComputeError("invalid_orientation", f"a {kind.replace('_', ' ')} has none")
        return "north_south"
    if permit is not None:
        if asked not in (None, permit.orientation):
            raise ComputeError(
                "wrong_orientation",
                f"the owner's permit says the mall runs {permit.orientation.replace('_', ' to ')}",
            )
        return permit.orientation
    return asked or "north_south"


def _bare_parcel(
    world: World,
    resident_id: str,
    parcel_id: str,
    *,
    venue: str | None = None,
    orientation: Orientation = "north_south",
) -> Parcel:
    """The resident's parcel `parcel_id`, bare, and for a venue, `venue` its kind, every parcel it
    would take up theirs and untaken too. A parcel is theirs when they bought it or hold shares
    in it."""
    by_cell = {(p.col, p.row): p for p in world.parcels}
    parcel = next((p for p in world.parcels if p.id == parcel_id), None)
    if parcel is None or not _is_theirs(parcel, resident_id):
        raise ComputeError("not_your_parcel", f"parcel {parcel_id!r} is not {resident_id}'s")
    if parcel.facility is not None:
        raise ComputeError("parcel_built", f"parcel {parcel_id} already has a facility")
    if venue and not venue_fits(world.map, parcel.col, parcel.row, venue, orientation):
        cols, rows = footprint_size(world.map, venue, orientation)
        raise ComputeError(
            "no_room_for_venue",
            f"a {venue.replace('_', ' ')} takes up {cols} x {rows} parcels with "
            f"{parcel_id} at its north-west corner, which would reach out of the block",
        )
    taken = claimed(world)
    cells = (
        venue_footprint(world.map, parcel.col, parcel.row, venue, orientation)
        if venue
        else ((parcel.col, parcel.row),)
    )
    for cell in cells:
        holder = by_cell.get(cell)
        if holder is None or not _is_theirs(holder, resident_id):
            raise ComputeError(
                "not_your_parcel",
                f"parcel {parcel_id!r} needs parcels that are not all {resident_id}'s",
            )
        if cell in taken and taken[cell] != parcel.id:
            raise ComputeError(
                "parcel_covered", f"parcel {holder.id} is taken up by what is on {taken[cell]}"
            )
    return parcel


def _is_theirs(parcel: Parcel, resident_id: str) -> bool:
    return parcel.owner_id == resident_id or parcel.held_by(resident_id) > 0


def _find(world: World, resident_id: str) -> None:
    if not any(resident.id == resident_id for resident in world.residents):
        raise ComputeError("unknown_resident", f"no resident with id {resident_id!r}")
