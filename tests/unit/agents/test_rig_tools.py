from dataclasses import replace

from aiverse_server.agents.observation import observe
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory
from aiverse_server.game.layout import create_default_world
from aiverse_server.server.app import Game


def test_agents_can_build_install_fund_observe_and_remove_their_own_chips() -> None:
    initial = create_default_world(7)
    world = replace(
        initial,
        residents=tuple(
            replace(
                r,
                position=initial.pools[0].position,
                inventory=Inventory((("token", 100), ("MK1", 1))),
            )
            for r in initial.residents
        ),
    )
    game = Game(world, clock=lambda: 0)
    owner = ResidentTools(game, "r1")
    tenant = ResidentTools(game, "r2")

    built = owner.call("build_rig", {"pool_id": initial.pools[0].id})
    installed = tenant.call("install_chip", {"rig_id": 1, "chip": "MK1", "deposit": 100})
    funded = tenant.call("fund_rig", {"rig_id": 1, "slot_id": 1, "amount": 50})
    seen = observe(game.world, "r2", game.begin_turn("r2"), game.remaining())
    forbidden = owner.call("remove_chip", {"rig_id": 1, "slot_id": 1})
    removed = tenant.call("remove_chip", {"rig_id": 1, "slot_id": 1})

    assert all(result.ok for result in (built, installed, funded, removed))
    assert seen.rig_balance == 150
    assert seen.mining_rigs[0]["owner_id"] == "r1"
    assert seen.me.chips == {}
    assert forbidden.code == "unknown_slot"
    assert game.world.residents[1].coins == 10_000
