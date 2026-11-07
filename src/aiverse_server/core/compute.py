"""Immutable compute state stored in the world: the nodes residents subscribe to and the jobs
running on any source of compute; rules live in economy/compute.py.

A job runs on a source of compute: the resident's own, `self`, or a node bought for one job
only, `on_demand`, or a node the resident subscribed to, `subscription`. Subscribed nodes are
the only ones that outlive a job, so they are the only ones stored.
"""

import math
from dataclasses import dataclass
from typing import Literal

NodeKind = Literal["generative", "render"]
"""What a node does. Generative nodes build facilities; render nodes make content."""

NODE_KINDS: tuple[NodeKind, ...] = ("generative", "render")

JobKind = Literal["build_ad", "build_portal", "make_content"]
"""What a job makes. It needs a node of the matching kind, see `economy/compute.py`."""

JobSource = Literal["self", "on_demand", "subscription"]


@dataclass(frozen=True)
class Node:
    """A node a resident subscribed to: theirs, one job at a time, until `expires_at`."""

    id: int
    owner_id: str
    kind: NodeKind
    bought_at: float
    expires_at: float
    """Game times when the subscription began and when it runs out, see `World.time`."""

    def __post_init__(self) -> None:
        if self.kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {self.kind!r}")
        if not (math.isfinite(self.bought_at) and math.isfinite(self.expires_at)):
            raise ValueError(f"node {self.id} has a time that is not finite")
        if not self.bought_at < self.expires_at:
            raise ValueError(f"node {self.id} runs out before it begins")


@dataclass(frozen=True)
class Job:
    """Work in progress that finishes at `ends_at` whether or not its owner is around."""

    id: int
    owner_id: str
    kind: JobKind
    parcel_id: str | None
    """The parcel the work builds on, see `core/land.py`; None for work that makes something
    to carry, such as content."""
    source: JobSource
    node_id: int | None
    """The subscribed node doing the work; None for the other sources, and for a subscription
    job whose node has run out and which carries on at the speed of the resident's own
    compute."""
    started_at: float
    ends_at: float

    def __post_init__(self) -> None:
        if self.kind not in ("build_ad", "build_portal", "make_content"):
            raise ValueError(f"unknown job kind {self.kind!r}")
        if (self.kind != "make_content") != (self.parcel_id is not None):
            raise ValueError(f"job {self.id} of kind {self.kind} has parcel {self.parcel_id!r}")
        if self.source != "subscription" and self.node_id is not None:
            raise ValueError(f"job {self.id} names a node but is not subscribed")
        if not (math.isfinite(self.started_at) and math.isfinite(self.ends_at)):
            raise ValueError(f"job {self.id} has a time that is not finite")
        if not self.started_at < self.ends_at:
            raise ValueError(f"job {self.id} ends before it starts")


@dataclass(frozen=True)
class ComputeBook:
    """Subscribed nodes and running jobs, oldest first, and the ids the next ones will get."""

    nodes: tuple[Node, ...] = ()
    jobs: tuple[Job, ...] = ()
    next_node_id: int = 1
    next_job_id: int = 1

    def __post_init__(self) -> None:
        for what, entries, next_id in (
            ("node", self.nodes, self.next_node_id),
            ("job", self.jobs, self.next_job_id),
        ):
            if type(next_id) is not int or next_id < 1:
                raise ValueError(f"next {what} id must be an integer >= 1, got {next_id!r}")
            previous = 0
            for entry in entries:
                if type(entry.id) is not int or not previous < entry.id < next_id:
                    raise ValueError(
                        f"{what} id {entry.id!r} must be an integer above {previous} "
                        f"and below {next_id}"
                    )
                previous = entry.id
        nodes = {node.id: node for node in self.nodes}
        busy_nodes: set[int] = set()
        self_workers: set[str] = set()
        parcels: set[str] = set()
        for job in self.jobs:
            if job.parcel_id is not None:
                if job.parcel_id in parcels:
                    raise ValueError(f"parcel {job.parcel_id} has more than one job")
                parcels.add(job.parcel_id)
            if job.node_id is not None:
                node = nodes.get(job.node_id)
                if node is None or node.owner_id != job.owner_id:
                    raise ValueError(f"job {job.id} runs on a node that is not its owner's")
                if job.node_id in busy_nodes:
                    raise ValueError(f"node {job.node_id} has more than one job")
                busy_nodes.add(job.node_id)
            if job.source == "self":
                if job.owner_id in self_workers:
                    raise ValueError(f"{job.owner_id} has more than one job on their own compute")
                self_workers.add(job.owner_id)
