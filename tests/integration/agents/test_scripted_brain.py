import asyncio

from aiverse_server.agents.observation import Observation, observe
from aiverse_server.agents.scripted_brain import ScriptedBrain
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Resident
from aiverse_server.economy.chips import CHIP_MODELS
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


def test_the_scripted_bot_earns_coins_on_the_default_map() -> None:
    """Plays a fast-forwarded game: each round every bot acts, then the clock jumps to the
    next walk or job that ends."""

    async def scenario() -> None:
        clock = FakeClock()
        game = Game(create_default_world(seed=7), 600, clock)
        brains = {resident.id: ScriptedBrain(seed=7) for resident in game.world.residents}
        while not game.world.ended:
            for resident_id, brain in brains.items():
                if not me(game, resident_id).left:
                    await brain.take_turn(look(game, resident_id), ResidentTools(game, resident_id))
            due = next_due(game.world)
            clock.now = min(due if due is not None else clock.now + 5, 600)
            game.tick()
        # Every bot bought a chip, mined and sold; whether chips pay off is a matter of
        # tuning, see docs/status.md in the client repository.
        assert all(not resident.left for resident in game.world.residents)
        assert all(
            any(resident.inventory.count(chip.model) for chip in CHIP_MODELS)
            for resident in game.world.residents
        )
        assert all(pool.reserve < 150 for pool in game.world.pools)
        assert game.world.market is not None and game.world.market.pressure > 0
        assert max(resident.coins for resident in game.world.residents) > STARTING_COINS

    asyncio.run(scenario())
