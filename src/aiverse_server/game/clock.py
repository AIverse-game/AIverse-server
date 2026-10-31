"""Game time: what falls due as `World.time` moves on.

Mining jobs and walks end at a set game time, and contracts lapse at one. The server's clock
(`app.Game.tick`) moves `World.time` on as real time passes, calls `settle_due` to finish
what has ended, and waits until `next_due` to look again; tests set the time themselves to
skip ahead. Ending the game on time is the caller's part, see `lifecycle.end_game`.
"""

from dataclasses import replace

from ..core.world import World
from ..economy.contracts import expire_contracts, next_expiry
from ..economy.mining import finish_mining, next_finish
from ..economy.rigs import finish_rigs, next_cycle
from ..residents.perception import record_activity
from ..residents.walking import finish_walks, next_arrival
from .lifecycle import knock_out_arrivals


def settle_due(world: World) -> World:
    """Pay out the mining jobs that have ended by `world.time` and bring in the walkers who
    have arrived, taking out of the game those left without tokens, all logged for others
    nearby to notice; then end the contracts that have lapsed. Returns `world` itself when
    nothing is due."""
    target = world.time
    after = world
    while (due := next_due(after)) is not None and due <= target:
        timed = replace(after, time=due)
        paid = finish_mining(timed)
        # An exhausted walker leaves before rigs at the same instant can pay them.
        arrived = knock_out_arrivals(paid, finish_walks(paid))
        after = finish_rigs(expire_contracts(record_activity(timed, arrived)))
    return world if after is world else replace(after, time=target)


def next_due(world: World) -> float | None:
    """Game time when the earliest mining job, walk or contract ends, or None when there is
    none."""
    due = (next_finish(world), next_arrival(world), next_expiry(world), next_cycle(world))
    times = [time for time in due if time is not None]
    return min(times, default=None)
