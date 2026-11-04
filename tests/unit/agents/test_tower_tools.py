from dataclasses import replace

from aiverse_server.agents.observation import observe
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory
from aiverse_server.game.layout import create_default_world
from aiverse_server.server.app import Game


def test_agents_can_build_install_fund_observe_and_remove_their_own_crystals() -> None:
    initial = create_default_world(7)
    world = replace(
        initial,
        residents=tuple(
            replace(
                r,
                position=initial.springs[0].position,
                inventory=Inventory((("energy", 100), ("C1", 1))),
            )
            for r in initial.residents
        ),
    )
    game = Game(world, clock=lambda: 0)
    owner = ResidentTools(game, "r1")
    tenant = ResidentTools(game, "r2")

    built = owner.call("build_tower", {"spring_id": initial.springs[0].id})
    installed = tenant.call("install_crystal", {"tower_id": 1, "crystal": "C1", "deposit": 100})
    funded = tenant.call("fund_tower", {"tower_id": 1, "slot_id": 1, "amount": 50})
    seen = observe(game.world, "r2", game.begin_turn("r2"), game.remaining())
    forbidden = owner.call("remove_crystal", {"tower_id": 1, "slot_id": 1})
    removed = tenant.call("remove_crystal", {"tower_id": 1, "slot_id": 1})

    assert all(result.ok for result in (built, installed, funded, removed))
    assert seen.tower_balance == 150
    assert seen.towers[0]["owner_id"] == "r1"
    assert seen.me.crystals == {}
    assert forbidden.code == "unknown_slot"
    assert game.world.residents[1].coins == 10_000


def test_building_a_tower_tells_the_agent_its_id() -> None:
    initial = create_default_world(7)
    spring = initial.springs[0]
    at_spring = replace(initial.residents[0], position=spring.position)
    neighbour = replace(initial.residents[1], position=spring.position)
    world = replace(initial, residents=(at_spring, neighbour, *initial.residents[2:]))
    game = Game(world, clock=lambda: 0)
    assert ResidentTools(game, "r2").call("build_tower", {"spring_id": spring.id}).ok

    built = ResidentTools(game, "r1").call("build_tower", {"spring_id": spring.id})

    assert built.ok
    assert built.message == "built tower 2; install crystals into it with tower_id 2"
