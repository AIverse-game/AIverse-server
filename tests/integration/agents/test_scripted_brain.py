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


def test_the_scripted_bots_tour_the_map_buy_a_parcel_and_build_ad_slots_nobody_passes() -> None:
    """Plays a fast-forwarded game: each round every bot acts, then the clock jumps to the
    next thing that falls due, at least 2 seconds on."""

    async def scenario() -> None:
        clock = FakeClock()
        game = Game(create_default_world(), 600, clock)
        brains = {resident.id: ScriptedBrain() for resident in game.world.residents}
        while not game.world.ended:
            for resident_id, brain in brains.items():
                if not me(game, resident_id).left:
                    await brain.take_turn(look(game, resident_id), ResidentTools(game, resident_id))
            due = next_due(game.world)
            # The visitors' steps are due many times a second; the bots need not look at each.
            clock.now = min(max(due if due is not None else clock.now + 5, clock.now + 2), 600)
            game.tick()
        owners = [parcel.owner_id for parcel in game.world.parcels]
        assert all(not resident.left for resident in game.world.residents)
        assert owners
        # Each bot bought one whole block of nine parcels, and no two bought the same.
        assert len(owners) == 9 * len(set(owners))
        # Each built one ad slot on the parcel it stood in, from its own compute, which costs
        # nothing.
        built = [parcel for parcel in game.world.parcels if parcel.facility is not None]
        assert sorted(parcel.owner_id for parcel in built) == sorted(set(owners))
        assert game.world.compute.jobs == ()
        # They keep their slots stocked with content from their own compute. Nobody goes to look
        # at an ad, and no venue stands anywhere in this game, so no visitor passes the slots and
        # sponsors pay nothing; only the land costs them coins.
        assert all(parcel.facility.contents for parcel in built if parcel.facility)
        paid = sum(t.amount for t in game.world.ledger.transfers if t.channel == "sponsors")
        spent = sum(t.amount for t in game.world.ledger.transfers if t.channel == "land")
        assert spent > 0 and paid == 0
        assert game.world.crowd.views == ()
        for parcel in game.world.parcels:
            owner = me(game, parcel.owner_id)
            assert owner.coins > STARTING_COINS - 9_000
        assert all(resident.position == Position(*TOUR[-1]) for resident in game.world.residents)

    asyncio.run(scenario())
