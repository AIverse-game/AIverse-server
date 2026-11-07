"""Game time: what falls due as `World.time` moves on.

Harvesting, gathering and writing jobs and walks end at a set game time, contracts lapse at
one, orders expire, and the market notes its price at regular times. The server's clock
(`app.Game.tick`) moves `World.time` on as real time passes, calls `settle_due` to finish what
has ended, and waits until `next_due` to look again; tests set the time themselves to skip
ahead. Ending the game on time is the caller's part, see `lifecycle.end_game`.
"""

from dataclasses import replace

from ..core.world import World
from ..economy.collectors import finish_collectors, next_cycle
from ..economy.contracts import expire_contracts, next_expiry
from ..economy.exchange import expire_orders, next_order_expiry
from ..economy.gathering import finish_gathering, next_gather_end
from ..economy.harvesting import finish_harvesting, next_finish
from ..economy.market import next_sample, sample_price
from ..economy.studio import finish_writing, next_write_end
from ..residents.perception import record_activity, record_discoveries
from ..residents.walking import finish_walks, next_arrival
from .lifecycle import knock_out_arrivals


def settle_due(world: World) -> World:
    """Pay out the harvesting, gathering and writing jobs that have ended by `world.time` and
    bring in the walkers who have arrived, taking out of the game those left without compute,
    all logged for others nearby to notice; the walkers discover the springs along their way,
    see `perception.record_discoveries`. End the contracts that have lapsed, withdraw the
    orders that have expired and note the market price when it is time. Returns `world`
    itself when nothing is due."""
    target = world.time
    after = world
    while (due := next_due(after)) is not None and due <= target:
        timed = replace(after, time=due)
        paid = finish_writing(finish_gathering(finish_harvesting(timed)))
        # An exhausted walker leaves before collectors at the same instant can pay them.
        arrived = knock_out_arrivals(paid, finish_walks(paid))
        logged = record_activity(timed, arrived)
        found = record_discoveries(paid, logged)
        after = sample_price(finish_collectors(expire_orders(expire_contracts(found))))
    return world if after is world else replace(after, time=target)


def next_due(world: World) -> float | None:
    """Game time when the earliest job, walk, contract, order expiry or price sample is due, or
    None when there is none."""
    due = (
        next_finish(world),
        next_gather_end(world),
        next_write_end(world),
        next_arrival(world),
        next_expiry(world),
        next_order_expiry(world),
        next_cycle(world),
        next_sample(world),
    )
    times = [time for time in due if time is not None]
    return min(times, default=None)
