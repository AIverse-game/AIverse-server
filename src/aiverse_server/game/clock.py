"""Game time: what falls due as `World.time` moves on.

Walks end at a set game time, contracts lapse at one and orders expire. The server's clock
(`app.Game.tick`) moves `World.time` on as real time passes, calls `settle_due` to finish what
has ended, and waits until `next_due` to look again; tests set the time themselves to skip
ahead. Ending the game on time is the caller's part, see `lifecycle.end_game`.
"""

from dataclasses import replace

from ..core.world import World
from ..economy.contracts import expire_contracts, next_expiry
from ..economy.exchange import expire_orders, next_order_expiry
from ..residents.perception import record_activity
from ..residents.walking import finish_walks, next_arrival


def settle_due(world: World) -> World:
    """Bring in the walkers who have arrived by `world.time`, logged for others nearby to
    notice, end the contracts that have lapsed and withdraw the orders that have expired.
    Returns `world` itself when nothing is due."""
    target = world.time
    after = world
    while (due := next_due(after)) is not None and due <= target:
        timed = replace(after, time=due)
        arrived = record_activity(timed, finish_walks(timed))
        after = expire_orders(expire_contracts(arrived))
    return world if after is world else replace(after, time=target)


def next_due(world: World) -> float | None:
    """Game time when the earliest walk, contract or order expiry is due, or None when there
    is none."""
    due = (next_arrival(world), next_expiry(world), next_order_expiry(world))
    times = [time for time in due if time is not None]
    return min(times, default=None)
