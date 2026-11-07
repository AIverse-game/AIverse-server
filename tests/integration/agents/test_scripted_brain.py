import asyncio

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.scripted_brain import TOUR, ScriptedBrain
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Position, Resident
from aiverse_server.game.clock import next_due
from aiverse_server.game.layout import STARTING_COINS, create_default_world
from aiverse_server.server.app import Game


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def look(game: Game, resident_id: str) -> Observation:
    # The turn first: it brings the world up to now.
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def me(game: Game, resident_id: str) -> Resident:
    return next(r for r in game.world.residents if r.id == resident_id)


def test_the_scripted_bots_tour_the_map_buy_a_parcel_and_build_an_ad_slot_on_it() -> None:
    """Plays a fast-forwarded game: each round every bot acts, then the clock jumps to the
    next walk that ends."""

    async def scenario() -> None:
        clock = FakeClock()
        game = Game(create_default_world(), 600, clock)
        brains = {resident.id: ScriptedBrain() for resident in game.world.residents}
        while not game.world.ended:
            for resident_id, brain in brains.items():
                if not me(game, resident_id).left:
                    await brain.take_turn(look(game, resident_id), ResidentTools(game, resident_id))
            due = next_due(game.world)
            clock.now = min(due if due is not None else clock.now + 5, 600)
            game.tick()
        owners = [parcel.owner_id for parcel in game.world.parcels]
        assert all(not resident.left for resident in game.world.residents)
        assert owners
        assert len(set(owners)) == len(owners)
        # Every parcel got its ad slot from its owner's own compute, which costs nothing.
        assert all(parcel.facility is not None for parcel in game.world.parcels)
        assert game.world.compute.jobs == ()
        # Nothing pays coins yet, so the only change is what was paid for land.
        spent = sum(STARTING_COINS - resident.coins for resident in game.world.residents)
        assert spent > 0
        assert all(resident.coins <= STARTING_COINS for resident in game.world.residents)
        assert all(resident.position == Position(*TOUR[-1]) for resident in game.world.residents)

    asyncio.run(scenario())
