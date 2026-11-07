"""Compute: how much work a resident can have done at a time, and what building costs.

Compute is a speed, not something carried: a piece of work has a workload, and the time it takes
is its workload over the rate of the compute doing it. Work runs on its source, so the resident
need not stay by it; they can walk, talk and trade while it runs, and it finishes without them
as long as they are still in the game. There are three sources:

- `self`: every resident's own compute, free but slow, `SELF_RATE`, one job at a time;
- `on_demand`: a system node hired for one job, `NODE_RATE`, paid for by the workload at
  `ON_DEMAND_PRICE`;
- `subscription`: a system node the resident subscribed to for a stretch of time with
  `buy_node`, theirs for that time and doing one job at a time at `NODE_RATE`, paid for by the
  second at `SUBSCRIPTION_PRICE` whether it is busy or not. Having several nodes runs several
  jobs at once. A job still running when the subscription runs out carries on at `SELF_RATE`,
  the speed of one's own compute, without using up the resident's own one-job-at-a-time slot.

Nodes have a kind, and a job needs a node of its kind: generative nodes build facilities. Render
nodes, for making content, come with the content design. The first job is `build_ad`: an ad slot
on a parcel the resident owns, wherever the resident is. Ad slots earn nothing yet.

Compute is bought from the system, so its price leaves the game, and nothing is paid back for
unused subscriptions, running jobs or facilities when a resident leaves or the game ends.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import replace
from typing import Literal

from ..core.compute import ComputeBook, Job, JobSource, Node, NodeKind
from ..core.inventory import take_coins
from ..core.land import Facility, Parcel
from ..core.ledger import SYSTEM, record_transfer
from ..core.world import World

SELF_RATE = 1.0
"""Workload a resident's own compute does per game second."""

NODE_RATE = 10.0
"""Workload a node does per game second."""

AD_WORKLOAD = 120.0
"""Workload of building an ad slot: two minutes of one's own compute, 12 seconds of a node."""

ON_DEMAND_PRICE = 3
"""Coins per unit of workload of a job on a node hired for it alone."""

SUBSCRIPTION_PRICE = 12
"""Coins per second of a subscribed node, busy or idle. A node that never rests builds an ad slot
in 12 seconds, so a subscription is cheaper per job than hiring once it is kept busy more than
a third of the time."""

NODE_SECONDS = (30, 600)
"""Least and most game seconds one subscription lasts."""

MAX_NODES = 4
"""Subscribed nodes one resident may hold at a time."""

ComputeErrorCode = Literal[
    "unknown_resident",
    "invalid_seconds",
    "too_many_nodes",
    "unknown_node_kind",
    "not_your_parcel",
    "parcel_built",
    "parcel_busy",
    "invalid_source",
    "source_busy",
    "unknown_node",
    "wrong_node_kind",
    "node_expired",
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


def buy_node(world: World, resident_id: str, kind: NodeKind, seconds: int) -> World:
    """Subscribe to a node of `kind` for `seconds` from now; the coins go to the system."""
    _find(world, resident_id)
    if kind != "generative":
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
    slot appears when the job ends."""
    _find(world, resident_id)
    parcel = _bare_parcel(world, resident_id, parcel_id)
    book = world.compute
    ends_at = world.time + job_seconds(AD_WORKLOAD, source)
    cost = 0
    if source == "subscription":
        if type(node_id) is not int:
            raise ComputeError("invalid_source", "a subscription job names its node by number")
        node = next((n for n in book.nodes if n.id == node_id), None)
        if node is None or node.owner_id != resident_id:
            raise ComputeError("unknown_node", f"resident {resident_id!r} has no node {node_id}")
        if node.kind != "generative":
            raise ComputeError("wrong_node_kind", f"node {node_id} cannot build facilities")
        if any(job.node_id == node_id for job in book.jobs):
            raise ComputeError("source_busy", f"node {node_id} is already at work")
        if node.expires_at <= world.time:
            raise ComputeError("node_expired", f"node {node_id} has run out")
        ends_at = subscription_job_end(AD_WORKLOAD, world.time, node.expires_at)
    else:
        if node_id is not None:
            raise ComputeError("invalid_source", f"{source!r} compute is not a node")
        if source == "self":
            if any(job.owner_id == resident_id and job.source == "self" for job in book.jobs):
                raise ComputeError("source_busy", "your own compute is already at work")
        elif source == "on_demand":
            cost = on_demand_cost(AD_WORKLOAD)
        else:
            raise ComputeError("invalid_source", f"no compute source {source!r}")
    if any(job.parcel_id == parcel.id for job in book.jobs):
        raise ComputeError("parcel_busy", f"parcel {parcel.id} already has a job running")
    if cost:
        world = take_coins(world, resident_id, cost)
        world = record_transfer(world, resident_id, SYSTEM, cost, "compute")
    job = Job(
        book.next_job_id,
        resident_id,
        "build_ad",
        parcel.id,
        source,
        node_id,
        world.time,
        ends_at,
    )
    book = replace(book, jobs=(*book.jobs, job), next_job_id=book.next_job_id + 1)
    return replace(world, compute=book)


def finish_jobs(world: World) -> World:
    """Finish every job due by `world.time`, oldest end first: its ad slot appears. The same
    world when none is due."""
    due = sorted(
        (job for job in world.compute.jobs if job.ends_at <= world.time),
        key=lambda job: (job.ends_at, job.id),
    )
    for job in due:
        parcels = tuple(
            replace(parcel, facility=Facility("ad", world.time))
            if parcel.id == job.parcel_id
            else parcel
            for parcel in world.parcels
        )
        book = replace(world.compute, jobs=tuple(j for j in world.compute.jobs if j.id != job.id))
        world = replace(world, parcels=parcels, compute=book)
    return world


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


def next_compute_due(world: World) -> float | None:
    """Game time when the earliest job ends or node runs out, or None when there is none."""
    book = world.compute
    times = [job.ends_at for job in book.jobs] + [node.expires_at for node in book.nodes]
    return min(times, default=None)


def cancel_compute(world: World, resident_id: str | None = None) -> World:
    """Drop the resident's jobs and nodes, or everyone's when `resident_id` is None. Nothing is
    paid back and no ad slot appears."""
    book = world.compute
    keep = ComputeBook(
        tuple(n for n in book.nodes if resident_id is not None and n.owner_id != resident_id),
        tuple(j for j in book.jobs if resident_id is not None and j.owner_id != resident_id),
        book.next_node_id,
        book.next_job_id,
    )
    return replace(world, compute=keep)


def _bare_parcel(world: World, resident_id: str, parcel_id: str) -> Parcel:
    parcel = next((p for p in world.parcels if p.id == parcel_id), None)
    if parcel is None or parcel.owner_id != resident_id:
        raise ComputeError("not_your_parcel", f"parcel {parcel_id!r} is not {resident_id}'s")
    if parcel.facility is not None:
        raise ComputeError("parcel_built", f"parcel {parcel_id} already has a facility")
    return parcel


def _find(world: World, resident_id: str) -> None:
    if not any(resident.id == resident_id for resident in world.residents):
        raise ComputeError("unknown_resident", f"no resident with id {resident_id!r}")
