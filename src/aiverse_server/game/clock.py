"""Game time: what falls due as `World.time` moves on.

Harvesting, digging and refining jobs and walks end at a set game time, contracts lapse at
one, and the market notes its price at regular times. The server's clock (`app.Game.tick`)
moves `World.time` on as real time passes, calls `settle_due` to finish what has ended, and
waits until `next_due` to look again; tests set the time themselves to skip ahead. Ending the
game on time is the caller's part, see `lifecycle.end_game`.
"""

from dataclasses import replace

from ..core.world import World
from ..economy.contracts import expire_contracts, next_expiry
from ..economy.harvesting import finish_harvesting, next_finish
from ..economy.market import next_sample, sample_price
from ..economy.shards import finish_digging, next_dig_end
from ..economy.towers import finish_towers, next_cycle
from ..economy.workshop import finish_refining, next_refine_end
from ..residents.perception import record_activity, record_discoveries
from ..residents.walking import finish_walks, next_arrival
from .lifecycle import knock_out_arrivals


def settle_due(world: World) -> World:
    """Pay out the harvesting, digging and refining jobs that have ended by `world.time` and
    bring in the walkers who
    have arrived, taking out of the game those left without energy, all logged for others
    nearby to notice; the walkers discover the springs along their way, see
    `perception.record_discoveries`. End the contracts that have lapsed and note the market
    price when it is time. Returns `world` itself when nothing is due."""
    target = world.time
    after = world
    while (due := next_due(after)) is not None and due <= target:
        timed = replace(after, time=due)
        paid = finish_refining(finish_digging(finish_harvesting(timed)))
        # An exhausted walker leaves before towers at the same instant can pay them.
        arrived = knock_out_arrivals(paid, finish_walks(paid))
        logged = record_activity(timed, arrived)
        found = record_discoveries(paid, logged)
        after = sample_price(finish_towers(expire_contracts(found)))
    return world if after is world else replace(after, time=target)


def next_due(world: World) -> float | None:
    """Game time when the earliest job, walk, contract or price sample is due, or None when
    there is none."""
    due = (
        next_finish(world),
        next_dig_end(world),
        next_refine_end(world),
        next_arrival(world),
        next_expiry(world),
        next_cycle(world),
        next_sample(world),
    )
    times = [time for time in due if time is not None]
    return min(times, default=None)
