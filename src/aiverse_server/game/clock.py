"""Game time: what falls due as `World.time` moves on.

Walks and compute jobs end at a set game time, subscribed nodes run out, visitors finish a
walk or a stay, the sponsors pay at fixed times, contracts lapse at one and orders expire.
The server's clock (`app.Game.tick`) moves `World.time` on as real time passes, calls
`settle_due` to finish what has ended, and waits until `next_due` to look again; tests set the
time themselves to skip ahead. Ending the game on time is the caller's part, see
`lifecycle.end_game`.
"""

from dataclasses import replace

from ..core.world import World
from ..economy.admissions import next_admission_payday, pay_admissions
from ..economy.compute import expire_nodes, expire_permits, finish_jobs, next_compute_due
from ..economy.contracts import expire_contracts, next_expiry
from ..economy.exchange import expire_orders, next_order_expiry
from ..economy.joint_purchase import expire_joint_purchases, next_joint_purchase_expiry
from ..economy.sponsors import next_payday, pay_sponsors
from ..economy.visitors import advance_crowd, next_visit
from ..residents.perception import record_activity
from ..residents.walking import finish_walks, next_arrival
from .world_log import record_finished_jobs


def settle_due(world: World) -> World:
    """Bring in the walkers who have arrived by `world.time`, logged for others nearby to
    notice, finish the compute jobs that are done, logged in the world log, drop the nodes that
    ran out and the permits that lapsed, move the visitors on, pay the ad slots their views, end
    the contracts that have lapsed, withdraw the orders that have expired and drop the joint
    purchases nobody answered in time. Returns `world`
    itself when nothing is due."""
    target = world.time
    after = world
    while (due := next_due(after)) is not None and due <= target:
        timed = replace(after, time=due)
        arrived = record_activity(timed, finish_walks(timed))
        finished = record_finished_jobs(arrived, finish_jobs(arrived))
        built = expire_permits(expire_nodes(finished))
        paid = pay_admissions(pay_sponsors(advance_crowd(built)))
        after = expire_joint_purchases(expire_orders(expire_contracts(paid)))
    return world if after is world else replace(after, time=target)


def next_due(world: World) -> float | None:
    """Game time when the earliest walk, compute job or node, visitor's step, payday,
    contract, order or joint purchase expiry is due, or None when there is none."""
    due = (
        next_arrival(world),
        next_compute_due(world),
        next_visit(world),
        next_payday(world),
        next_admission_payday(world),
        next_expiry(world),
        next_order_expiry(world),
        next_joint_purchase_expiry(world),
    )
    times = [time for time in due if time is not None]
    return min(times, default=None)
