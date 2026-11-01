from dataclasses import replace

import pytest

from aiverse_server.core.world import Inventory, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.rigs import RIG_PRICE, pool_draw_rate, slot_status
from aiverse_server.game.actions import (
    Action,
    ActionRejected,
    BuildRig,
    FundRig,
    InstallChip,
    LeaveGame,
    Mine,
    MoveTo,
    RemoveChip,
    Say,
    perform,
)
from aiverse_server.game.clock import settle_due
from aiverse_server.game.lifecycle import end_game, total_coins
from aiverse_server.server.app import Game
from aiverse_server.server.protocol import world_view


def resident(id: str) -> Resident:
    return Resident(
        id,
        id,
        "#123456",
        Position(10, 10),
        Inventory((("token", 100), ("MK1", 5), ("MK3", 1))),
        10_000,
    )


WORLD = World(
    WorldMap(100),
    (resident("a"), resident("b"), resident("c")),
    pools=(Pool("p1", Position(10, 10), 4, 1000),),
)


def installed(tenant: str = "b", deposit: int = 500, chip: str = "MK1") -> World:
    return perform(perform(WORLD, BuildRig("a", "p1")), InstallChip(tenant, 1, chip, deposit))


def advance(world: World, time: float) -> World:
    return settle_due(replace(world, time=time))


def test_building_pays_for_a_finite_capacity_machine_without_claiming_the_pool() -> None:
    built = perform(WORLD, BuildRig("a", "p1"))

    assert built.residents[0].coins == 10_000 - RIG_PRICE
    assert built.rigs[0].owner_id == "a"
    assert built.pools == WORLD.pools
    assert WORLD.rigs == ()


def test_renter_receives_tokens_and_pays_owner_only_for_actual_production() -> None:
    world = installed()

    before = advance(world, 19)
    paid = advance(before, 20)

    assert before.residents[1].inventory.count("token") == 100
    # An MK1 mines 30 a job, twice that in a rig.
    assert paid.residents[1].inventory.count("token") == 160
    assert paid.pools[0].reserve == 940
    assert (
        paid.residents[0].coins == 10_000 - RIG_PRICE + 30
    )  # 60 * 0.5 rent, from the renter's escrow.
    assert paid.rigs[0].slots[0].balance == 458  # 30 rent + 12 electricity.
    assert paid.rigs[0].slots[0].rent_paid == 30
    assert total_coins(paid, "b") == 9958
    assert sum(total_coins(paid, r.id) for r in paid.residents) == 30_000 - RIG_PRICE - 12


def test_owners_chips_only_pay_electricity_and_do_not_keep_them_busy() -> None:
    world = installed("a")

    walking = perform(world, MoveTo("a", Position(50, 10)))
    paid = advance(walking, 20)

    assert paid.residents[0].position == Position(50, 10)
    assert paid.residents[0].inventory.count("token") == 120  # 100 - 40 walk + 60 mined.
    assert paid.rigs[0].slots[0].balance == 488
    assert paid.rigs[0].slots[0].rent_paid == 0


def test_prepayment_stays_in_rankings_but_is_not_spendable() -> None:
    world = installed()
    view = world_view(world, 100)

    assert total_coins(world, "b") == 10_000
    assert view.residents[1].spendable_coins == 9500
    assert view.residents[1].rig_balance == 500
    assert view.residents[1].carried_chips["MK1"] == 4


def test_installed_chip_cannot_also_be_used_for_manual_mining() -> None:
    world = replace(
        WORLD,
        residents=(replace(resident("a"), inventory=Inventory((("token", 100), ("MK1", 1)))),),
    )
    world = perform(perform(world, BuildRig("a", "p1")), InstallChip("a", 1, "MK1", 500))

    with pytest.raises(ActionRejected) as raised:
        perform(world, Mine("a", "p1"))

    assert raised.value.code == "no_chip"


def test_installing_into_a_rig_elsewhere_names_the_pool_to_go_to() -> None:
    world = perform(WORLD, BuildRig("a", "p1"))
    away = replace(
        world,
        residents=(
            world.residents[0],
            replace(resident("b"), position=Position(50, 50)),
            world.residents[2],
        ),
    )

    with pytest.raises(ActionRejected) as raised:
        perform(away, InstallChip("b", 1, "MK1", 500))

    assert raised.value.code == "not_at_pool"
    assert raised.value.message == "rig 1 is at pool p1; stand there"


def test_insufficient_balance_pauses_without_charging_or_consuming_the_pool() -> None:
    world = installed(deposit=41)  # A cycle costs 42.

    paused = advance(world, 200)

    assert paused.residents[1].inventory.count("token") == 100
    assert paused.pools[0].reserve == 1000
    assert paused.rigs[0].slots[0].balance == 41
    assert paused.residents[0].coins == 10_000 - RIG_PRICE
    assert slot_status(paused, paused.rigs[0], paused.rigs[0].slots[0]) == "no_funds"


def test_pool_draw_rate_counts_only_running_slots() -> None:
    running = installed()  # One MK1 slot takes 60 tokens every 20 seconds.
    paused = installed(deposit=41)  # A cycle costs 42.

    assert pool_draw_rate(running, "p1") == 3
    assert pool_draw_rate(paused, "p1") == 0
    assert pool_draw_rate(WORLD, "p1") == 0


def test_recharging_a_paused_slot_waits_a_full_cycle_without_catching_up() -> None:
    world = advance(installed(deposit=1), 105)

    funded = perform(world, FundRig("b", 1, 1, 41))
    waiting = advance(funded, 124)
    paid = advance(waiting, 125)

    assert waiting.residents[1].inventory.count("token") == 100
    assert paid.residents[1].inventory.count("token") == 160
    assert paid.rigs[0].slots[0].balance == 0


def test_running_slot_keeps_its_schedule_when_topped_up() -> None:
    world = advance(installed(), 19)

    paid = advance(perform(world, FundRig("b", 1, 1, 1)), 20)

    assert paid.residents[1].inventory.count("token") == 160


def test_removal_returns_the_chip_and_all_unused_prepaid_coins() -> None:
    world = advance(installed(), 20)

    removed = perform(world, RemoveChip("b", 1, 1))
    later = advance(removed, 100)

    assert later.rigs[0].slots == ()
    assert later.residents[1].inventory.count("MK1") == 5
    assert later.residents[1].coins == 9958
    assert later.residents[1].inventory.count("token") == 160


def test_renter_leaving_stops_their_chip_and_keeps_the_machine_open() -> None:
    world = installed()

    left = perform(world, LeaveGame("b"))
    later = advance(left, 100)

    assert later.residents[1].left
    assert later.residents[1].inventory.stacks == ()
    assert later.residents[1].coins == 10_000
    assert later.residents[0].coins == 10_000 - RIG_PRICE
    assert later.rigs[0].slots == ()


def test_owner_leaving_closes_the_machine_and_refunds_other_players() -> None:
    world = advance(installed(), 20)

    left = perform(world, LeaveGame("a"))
    later = advance(left, 200)

    assert later.rigs == ()
    assert later.residents[1].inventory.count("MK1") == 5
    assert later.residents[1].inventory.count("token") == 160
    assert later.residents[1].coins == 9958
    assert later.residents[0].coins == 10_000 - RIG_PRICE + 30


@pytest.mark.parametrize("who", ["a", "b"])
def test_spending_the_last_token_stops_rigs_even_with_future_income(who: str) -> None:
    world = installed()
    world = replace(
        world,
        residents=tuple(
            replace(r, inventory=Inventory((("token", 1),))) if r.id == who else r
            for r in world.residents
        ),
    )

    left = perform(world, Say(who, "bye", "c"))
    later = advance(left, 200)

    assert next(r for r in later.residents if r.id == who).left
    assert later.residents[0].coins == 10_000 - RIG_PRICE
    assert later.residents[1].inventory.count("token") == (0 if who == "b" else 100)


def test_a_large_time_jump_does_not_mine_after_an_exhausted_walker_leaves() -> None:
    world = perform(installed(), MoveTo("b", Position(70, 90)))  # 100 tokens, arrives at 40.

    # This slot is unfunded until after the arrival; no early production saves the walker.
    world = replace(
        world, rigs=(replace(world.rigs[0], slots=(replace(world.rigs[0].slots[0], next_at=60),)),)
    )
    later = advance(world, 100)

    assert later.residents[1].left
    assert later.residents[1].coins == 10_000
    assert later.residents[0].coins == 10_000 - RIG_PRICE
    assert later.rigs[0].slots == ()


def test_production_at_the_same_instant_as_exhausted_arrival_does_not_revive() -> None:
    world = replace(
        WORLD,
        residents=(
            WORLD.residents[0],
            replace(WORLD.residents[1], inventory=Inventory((("token", 50), ("MK1", 1)))),
            WORLD.residents[2],
        ),
    )
    world = perform(perform(world, BuildRig("a", "p1")), InstallChip("b", 1, "MK1", 500))
    world = perform(world, MoveTo("b", Position(60, 10)))  # Arrival and first cycle at 20.

    later = advance(world, 100)

    assert later.residents[1].left
    assert later.residents[0].coins == 10_000 - RIG_PRICE


def test_game_end_refunds_prepayments_and_never_pays_after_the_deadline() -> None:
    world = installed()
    now = [0.0]
    game = Game(world, duration=39, clock=lambda: now[0])

    now[0] = 200
    game.tick()
    frozen = advance(game.world, 300)

    assert game.world.ended
    assert frozen.residents[1].inventory.count("token") == 160
    assert frozen.residents[1].coins == 9958
    assert frozen.rigs[0].slots == ()


def test_last_partial_cycle_charges_only_for_available_tokens() -> None:
    world = installed()
    world = replace(world, pools=(replace(world.pools[0], reserve=7),))

    paid = advance(world, 200)

    assert paid.residents[1].inventory.count("token") == 107
    assert paid.residents[0].coins == 10_000 - RIG_PRICE + 4
    assert paid.rigs[0].slots[0].balance == 494  # ceil(7 * .5) rent + ceil(7 * .2) energy.
    assert paid.pools[0].reserve == 0


def test_manual_jobs_and_rigs_cannot_duplicate_finite_tokens() -> None:
    world = installed()
    world = replace(world, pools=(replace(world.pools[0], reserve=40),))

    manual = perform(world, Mine("c", "p1"))  # MK3 reserves all remaining tokens.
    paid = advance(manual, 100)

    assert paid.pools[0].reserve == 0
    assert paid.residents[2].inventory.count("token") == 140
    assert paid.residents[1].inventory.count("token") == 100
    assert paid.rigs[0].slots[0].balance == 500


def test_large_and_small_clock_steps_produce_the_same_rental_settlement() -> None:
    world = perform(installed(), InstallChip("c", 1, "MK3", 1000))

    bulk = advance(world, 100)
    stepped = world
    for time in range(1, 101):
        stepped = advance(stepped, time)

    assert bulk == stepped


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (InstallChip("b", 1, "MK4", 500), "unknown_chip"),
        (InstallChip("b", 2, "MK1", 500), "unknown_rig"),
        (InstallChip("b", 1, "MK1", 0), "invalid_deposit"),
        (InstallChip("b", 1, "MK1", True), "invalid_deposit"),
        (InstallChip("b", 1, "MK1", 10001), "not_enough_coins"),
        (FundRig("c", 1, 1, 1), "unknown_slot"),
        (RemoveChip("a", 1, 1), "unknown_slot"),
        (BuildRig("a", "missing"), "unknown_pool"),
    ],
)
def test_invalid_requests_are_rejected_without_changing_resources(
    action: Action, code: str
) -> None:
    world = installed()

    with pytest.raises(ActionRejected) as raised:
        perform(world, action)

    assert raised.value.code == code
    assert world == installed()


def test_fifth_chip_is_rejected() -> None:
    world = installed()
    for _ in range(3):
        world = perform(world, InstallChip("b", 1, "MK1", 1))

    with pytest.raises(ActionRejected) as raised:
        perform(world, InstallChip("b", 1, "MK1", 1))

    assert raised.value.code == "rig_full"


@pytest.mark.parametrize("action", [BuildRig("b", "p1"), InstallChip("b", 1, "MK1", 1)])
def test_building_and_installing_require_standing_at_the_pool(action: Action) -> None:
    world = installed()
    world = replace(
        world,
        residents=tuple(
            replace(r, position=Position(90, 90)) if r.id == "b" else r for r in world.residents
        ),
    )

    with pytest.raises(ActionRejected) as raised:
        perform(world, action)

    assert raised.value.code == "not_at_pool"


def test_end_game_itself_does_not_run_pending_cycles() -> None:
    world = installed()

    ended = end_game(world)

    assert ended.rigs[0].slots == ()
    assert ended.residents[1].inventory == WORLD.residents[1].inventory
    assert ended.residents[1].coins == 10000
