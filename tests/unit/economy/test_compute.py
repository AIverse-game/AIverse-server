from dataclasses import replace

import pytest

from aiverse_server.core.compute import ComputeBook, Job, Node
from aiverse_server.core.inventory import coin_balance
from aiverse_server.core.land import Facility, Parcel
from aiverse_server.core.ledger import SYSTEM
from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.compute import (
    AD_WORKLOAD,
    MAX_NODES,
    NODE_RATE,
    NODE_SECONDS,
    ON_DEMAND_PRICE,
    SELF_RATE,
    SUBSCRIPTION_PRICE,
    next_compute_due,
)
from aiverse_server.game.actions import (
    ActionRejected,
    BuildAd,
    BuyNode,
    LeaveGame,
    MoveTo,
    perform,
)
from aiverse_server.game.clock import next_due, settle_due
from aiverse_server.game.lifecycle import end_game, total_coins

SELF_SECONDS = AD_WORKLOAD / SELF_RATE
NODE_JOB_SECONDS = AD_WORKLOAD / NODE_RATE


def resident(resident_id: str, coins: int = 10_000) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(5, 5), coins=coins)


# a owns two bare parcels, b one; a is far from all of them, which makes no difference.
WORLD = World(
    map=WorldMap(size=100),
    residents=(resident("a"), resident("b")),
    parcels=(Parcel(5, 3, "a"), Parcel(6, 3, "a"), Parcel(1, 1, "b")),
)


def run(world: World, *actions: BuyNode | BuildAd | MoveTo | LeaveGame) -> World:
    for action in actions:
        world = perform(world, action)
    return world


def facility(world: World, parcel_id: str) -> Facility | None:
    return next(p.facility for p in world.parcels if p.id == parcel_id)


def test_the_resident_s_own_compute_builds_an_ad_slot_slowly_and_for_free() -> None:
    started = perform(WORLD, BuildAd("a", "L5-3", "self"))

    before = settle_due(replace(started, time=SELF_SECONDS - 0.1))
    after = settle_due(replace(started, time=SELF_SECONDS))

    assert coin_balance(started, "a") == 10_000
    assert facility(before, "L5-3") is None
    assert facility(after, "L5-3") == Facility("ad", SELF_SECONDS)
    assert after.compute.jobs == ()
    assert next_due(started) == SELF_SECONDS


def test_a_hired_node_is_fast_and_paid_by_the_workload() -> None:
    started = perform(WORLD, BuildAd("a", "L5-3", "on_demand"))

    done = settle_due(replace(started, time=NODE_JOB_SECONDS))

    cost = round(AD_WORKLOAD * ON_DEMAND_PRICE)
    assert coin_balance(done, "a") == 10_000 - cost
    assert facility(done, "L5-3") is not None
    last = done.ledger.transfers[-1]
    assert (last.payer, last.payee, last.amount, last.channel) == ("a", SYSTEM, cost, "compute")


def test_a_subscribed_node_costs_by_the_second_and_runs_one_job_at_a_time() -> None:
    world = perform(WORLD, BuyNode("a", "generative", 60))
    world = perform(world, BuildAd("a", "L5-3", "subscription", node_id=1))

    with pytest.raises(ActionRejected) as busy:
        perform(world, BuildAd("a", "L6-3", "subscription", node_id=1))
    next_up = settle_due(replace(world, time=NODE_JOB_SECONDS))
    next_up = perform(next_up, BuildAd("a", "L6-3", "subscription", node_id=1))

    assert coin_balance(world, "a") == 10_000 - 60 * SUBSCRIPTION_PRICE
    assert busy.value.code == "source_busy"
    assert facility(next_up, "L5-3") is not None
    assert [job.parcel_id for job in next_up.compute.jobs] == ["L6-3"]


def test_two_nodes_work_at_once_and_so_do_a_node_and_the_residents_own_compute() -> None:
    world = run(
        WORLD,
        BuyNode("a", "generative", 60),
        BuyNode("a", "generative", 60),
        BuildAd("a", "L5-3", "subscription", node_id=1),
        BuildAd("a", "L6-3", "subscription", node_id=2),
    )

    done = settle_due(replace(world, time=NODE_JOB_SECONDS))

    assert [facility(done, parcel_id) is not None for parcel_id in ("L5-3", "L6-3")] == [True, True]


def test_a_job_that_outlasts_its_node_carries_on_at_the_residents_own_speed() -> None:
    world = perform(WORLD, BuyNode("a", "generative", NODE_SECONDS[0]))
    start = NODE_SECONDS[0] - 6.0
    late = replace(world, time=start)

    started = perform(late, BuildAd("a", "L5-3", "subscription", node_id=1))

    # 6 s on the node does 60 of the 120 workload; the other 60 take 60 s at the own speed.
    job = started.compute.jobs[0]
    assert job.ends_at == NODE_SECONDS[0] + (AD_WORKLOAD - 6.0 * NODE_RATE) / SELF_RATE
    mid = settle_due(replace(started, time=NODE_SECONDS[0]))
    assert mid.compute.nodes == ()
    assert [(j.source, j.node_id) for j in mid.compute.jobs] == [("subscription", None)]
    assert facility(settle_due(replace(started, time=job.ends_at - 0.1)), "L5-3") is None
    assert facility(settle_due(replace(started, time=job.ends_at)), "L5-3") is not None
    assert next_due(mid) == job.ends_at


def test_a_job_that_carries_on_does_not_use_up_the_residents_own_compute() -> None:
    world = perform(WORLD, BuyNode("a", "generative", NODE_SECONDS[0]))
    started = perform(
        replace(world, time=NODE_SECONDS[0] - 1.0), BuildAd("a", "L5-3", "subscription", node_id=1)
    )
    later = settle_due(replace(started, time=NODE_SECONDS[0]))

    own = perform(later, BuildAd("a", "L6-3", "self"))

    assert {job.parcel_id for job in own.compute.jobs} == {"L5-3", "L6-3"}


def test_a_job_ending_exactly_when_its_node_runs_out_still_finishes() -> None:
    world = perform(WORLD, BuyNode("a", "generative", 30))
    started = perform(
        replace(world, time=30.0 - NODE_JOB_SECONDS),
        BuildAd("a", "L5-3", "subscription", node_id=1),
    )

    done = settle_due(replace(started, time=30.0))

    assert facility(done, "L5-3") == Facility("ad", 30.0)
    assert done.compute == ComputeBook((), (), 2, 2)


def test_leaving_drops_a_job_that_carries_on_without_its_node() -> None:
    world = perform(WORLD, BuyNode("a", "generative", NODE_SECONDS[0]))
    started = perform(
        replace(world, time=NODE_SECONDS[0] - 1.0), BuildAd("a", "L5-3", "subscription", node_id=1)
    )
    tail = settle_due(replace(started, time=NODE_SECONDS[0]))

    left = perform(tail, LeaveGame("a"))

    assert left.compute.jobs == ()
    assert facility(settle_due(replace(left, time=500.0)), "L5-3") is None


def test_a_job_that_is_not_slowed_when_it_fits_in_the_subscription_keeps_its_node() -> None:
    world = perform(WORLD, BuyNode("a", "generative", 60))

    started = perform(world, BuildAd("a", "L5-3", "subscription", node_id=1))

    assert started.compute.jobs[0].node_id == 1


def test_a_job_that_fits_in_the_subscription_is_not_slowed() -> None:
    world = perform(WORLD, BuyNode("a", "generative", 60))

    started = perform(world, BuildAd("a", "L5-3", "subscription", node_id=1))

    assert started.compute.jobs[0].ends_at == NODE_JOB_SECONDS


def test_a_node_that_has_run_out_cannot_start_a_job() -> None:
    world = perform(WORLD, BuyNode("a", "generative", NODE_SECONDS[0]))
    over = replace(world, time=NODE_SECONDS[0])

    with pytest.raises(ActionRejected) as rejected:
        perform(over, BuildAd("a", "L5-3", "subscription", node_id=1))

    assert rejected.value.code == "node_expired"


def test_a_node_runs_out_when_its_time_is_up_after_its_last_job_finished() -> None:
    world = run(
        WORLD, BuyNode("a", "generative", 30), BuildAd("a", "L5-3", "subscription", node_id=1)
    )

    working = settle_due(replace(world, time=29.0))
    over = settle_due(replace(world, time=30.0))

    assert len(working.compute.nodes) == 1
    assert over.compute.nodes == ()
    assert facility(over, "L5-3") is not None
    assert next_compute_due(over) is None


def test_the_resident_may_walk_while_the_job_runs() -> None:
    world = run(WORLD, BuildAd("a", "L5-3", "self"), MoveTo("a", Position(50, 50)))

    done = settle_due(replace(world, time=SELF_SECONDS))

    assert facility(done, "L5-3") is not None
    assert done.residents[0].position == Position(50, 50)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (BuildAd("a", "L1-1", "self"), "not_your_parcel"),
        (BuildAd("a", "L9-9", "self"), "not_your_parcel"),
        (BuildAd("nobody", "L5-3", "self"), "unknown_resident"),
        (BuildAd("a", "L5-3", "self", node_id=1), "invalid_source"),
        (BuildAd("a", "L5-3", "subscription"), "invalid_source"),
        (BuildAd("a", "L5-3", "subscription", node_id=7), "unknown_node"),
        (BuildAd("a", "L5-3", "teleport"), "invalid_source"),  # type: ignore[arg-type]
        (BuyNode("a", "generative", NODE_SECONDS[0] - 1), "invalid_seconds"),
        (BuyNode("a", "generative", NODE_SECONDS[1] + 1), "invalid_seconds"),
        (BuyNode("a", "render", 60), "unknown_node_kind"),  # type: ignore[arg-type]
        (BuyNode("nobody", "generative", 60), "unknown_resident"),
    ],
)
def test_work_that_cannot_start_is_refused_and_changes_nothing(
    action: BuildAd | BuyNode, code: str
) -> None:
    with pytest.raises(ActionRejected) as rejected:
        perform(WORLD, action)

    assert rejected.value.code == code


def test_a_node_of_another_resident_cannot_be_used() -> None:
    world = perform(WORLD, BuyNode("b", "generative", 60))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuildAd("a", "L5-3", "subscription", node_id=1))

    assert rejected.value.code == "unknown_node"


def test_one_job_at_a_time_per_source_and_per_parcel() -> None:
    world = perform(WORLD, BuildAd("a", "L5-3", "self"))

    with pytest.raises(ActionRejected) as own:
        perform(world, BuildAd("a", "L6-3", "self"))
    with pytest.raises(ActionRejected) as parcel:
        perform(world, BuildAd("a", "L5-3", "on_demand"))

    assert (own.value.code, parcel.value.code) == ("source_busy", "parcel_busy")


def test_a_parcel_with_an_ad_slot_is_refused() -> None:
    built = settle_due(replace(perform(WORLD, BuildAd("a", "L5-3", "on_demand")), time=20.0))

    with pytest.raises(ActionRejected) as rejected:
        perform(built, BuildAd("a", "L5-3", "self"))

    assert rejected.value.code == "parcel_built"


def test_paid_work_needs_the_coins_and_a_refusal_keeps_them() -> None:
    poor = replace(WORLD, residents=(resident("a", coins=100), resident("b")))

    with pytest.raises(ActionRejected) as hire:
        perform(poor, BuildAd("a", "L5-3", "on_demand"))
    with pytest.raises(ActionRejected) as subscribe:
        perform(poor, BuyNode("a", "generative", 60))

    assert (hire.value.code, subscribe.value.code) == ("not_enough_coins",) * 2


def test_a_resident_holds_only_so_many_nodes() -> None:
    world = run(WORLD, *(BuyNode("a", "generative", 30) for _ in range(MAX_NODES)))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuyNode("a", "generative", 30))

    assert rejected.value.code == "too_many_nodes"


def test_leaving_drops_the_residents_jobs_and_nodes_without_a_refund_or_a_build() -> None:
    world = run(
        WORLD, BuyNode("a", "generative", 60), BuildAd("a", "L5-3", "subscription", node_id=1)
    )
    other = perform(world, BuyNode("b", "generative", 60))

    left = perform(other, LeaveGame("a"))
    later = settle_due(replace(left, time=NODE_JOB_SECONDS))

    assert [node.owner_id for node in left.compute.nodes] == ["b"]
    assert left.compute.jobs == ()
    assert facility(later, "L5-3") is None
    assert coin_balance(left, "a") == 10_000 - 60 * SUBSCRIPTION_PRICE


def test_the_end_of_the_game_drops_every_job_and_node() -> None:
    world = run(WORLD, BuyNode("b", "generative", 60), BuildAd("a", "L5-3", "self"))

    ended = end_game(world)

    assert ended.compute.nodes == () and ended.compute.jobs == ()
    assert facility(settle_due(replace(ended, time=500.0)), "L5-3") is None


def test_compute_prices_are_not_part_of_the_ranking_total() -> None:
    world = perform(WORLD, BuildAd("a", "L5-3", "on_demand"))

    assert total_coins(world, "a") == coin_balance(world, "a") == 10_000 - 360


NODE = Node(1, "a", "generative", 0.0, 60.0)
JOB = Job(1, "a", "build_ad", "L5-3", "subscription", 1, 0.0, 12.0)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Node(1, "a", "generative", 5.0, 5.0), "runs out before it begins"),
        (lambda: Node(1, "a", "render", 0.0, 5.0), "unknown node kind"),  # type: ignore[arg-type]
        (lambda: replace(JOB, source="self"), "names a node but is not subscribed"),
        (lambda: replace(JOB, ends_at=0.0), "ends before it starts"),
        (lambda: Facility("ad", -1.0), "built at"),
        (lambda: ComputeBook((NODE,), (JOB, replace(JOB, id=2)), 2, 3), "more than one job"),
        (lambda: ComputeBook((), (JOB,), 1, 2), "not its owner's"),
        (lambda: ComputeBook((NODE,), (JOB,), 2, 1), "must be an integer above"),
        (
            lambda: ComputeBook(
                (),
                (
                    replace(JOB, id=1, source="self", node_id=None),
                    replace(JOB, id=2, source="self", node_id=None, parcel_id="L6-3"),
                ),
                1,
                3,
            ),
            "more than one job on their own",
        ),
    ],
)
def test_impossible_compute_state_is_rejected(build: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()  # type: ignore[operator]


def test_a_world_rejects_jobs_and_facilities_it_cannot_account_for() -> None:
    stray = Job(1, "a", "build_ad", "L1-1", "self", None, 0.0, 12.0)
    own = replace(stray, parcel_id="L5-3")
    gone = replace(WORLD, residents=(replace(WORLD.residents[0], left=True), WORLD.residents[1]))
    built = (Parcel(5, 3, "a", Facility("ad", 0.0)), *WORLD.parcels[1:])

    with pytest.raises(ValueError, match="not its owner's"):
        replace(WORLD, compute=ComputeBook((), (stray,), 1, 2))
    with pytest.raises(ValueError, match="outlives its game or resident"):
        replace(gone, compute=ComputeBook((NODE,), (), 2, 1))
    with pytest.raises(ValueError, match="built in the future"):
        replace(WORLD, time=1.0, parcels=(Parcel(5, 3, "a", Facility("ad", 5.0)),))
    with pytest.raises(ValueError, match="started after the current time"):
        replace(WORLD, compute=ComputeBook((), (replace(own, started_at=5.0, ends_at=9.0),), 1, 2))
    with pytest.raises(ValueError, match="bought after the current time"):
        replace(WORLD, compute=ComputeBook((replace(NODE, bought_at=5.0),), (), 2, 1))
    with pytest.raises(ValueError, match="already has a facility"):
        replace(WORLD, parcels=built, compute=ComputeBook((), (own,), 1, 2))


def test_two_jobs_on_one_parcel_are_rejected() -> None:
    first = Job(1, "a", "build_ad", "L5-3", "self", None, 0.0, 12.0)
    second = replace(first, id=2, source="on_demand")

    with pytest.raises(ValueError, match="more than one job"):
        ComputeBook((), (first, second), 1, 3)


def test_a_subscription_job_must_name_its_node_by_a_whole_number() -> None:
    world = perform(WORLD, BuyNode("a", "generative", 60))

    with pytest.raises(ActionRejected) as rejected:
        perform(world, BuildAd("a", "L5-3", "subscription", node_id=True))

    assert rejected.value.code == "invalid_source"
