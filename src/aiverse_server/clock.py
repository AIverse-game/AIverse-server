"""Game time: what falls due as `World.time` moves on.

Mining jobs and walks end at a set game time. The server's clock (`app.Game.tick`) moves
`World.time` on as real time passes, calls `settle_due` to finish what has ended, and waits
until `next_due` to look again; tests set the time themselves to skip ahead. Ending the game
on time is the caller's part, see `lifecycle.end_game`.
"""

from .lifecycle import knock_out_arrivals
from .mining import finish_mining, next_finish
from .perception import record_activity
from .walking import finish_walks, next_arrival
from .world import World


def settle_due(world: World) -> World:
    """Pay out the mining jobs that have ended by `world.time` and bring in the walkers who
    have arrived, taking out of the game those left without tokens, all logged for others
    nearby to notice. Returns `world` itself when nothing is due."""
    paid = finish_mining(world)
    arrived = knock_out_arrivals(paid, finish_walks(paid))
    return record_activity(world, arrived)


def next_due(world: World) -> float | None:
    """Game time when the earliest mining job or walk ends, or None when there is none."""
    times = [time for time in (next_finish(world), next_arrival(world)) if time is not None]
    return min(times, default=None)
