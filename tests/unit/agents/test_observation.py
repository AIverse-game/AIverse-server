import json
from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.core.world import Inventory, Market, Pool, Position, Resident, World, WorldMap
from aiverse_server.economy.tokens import TOKEN
from aiverse_server.game.actions import BuildRig, InstallChip, Mine, MoveTo, OfferToSell, perform
from aiverse_server.server.app import Game

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((TOKEN, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((TOKEN, 100),)), coins=5000),
    ),
    pools=(Pool("p1", Position(80, 50), 4, 300),),
    market=Market(Position(50, 50), 5),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    # The turn first: it brings the world up to now.
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_observation_shows_self_market_pools_offers_and_ranking() -> None:
    game, _ = make_game()
    game.act(OfferToSell("b", "a", TOKEN, 5, 80))

    seen = look(game, "a")
    them = look(game, "b")

    assert seen.me.at_market and seen.me.tokens == 100 and seen.me.coins == 5000
    assert seen.market is not None and seen.market.distance == 0
    assert seen.offers_to_me[0].from_id == "b" and seen.offers_to_me[0].side == "sell"
    assert [standing.id for standing in seen.residents] == ["a", "b"]
    assert [resident.id for resident in seen.nearby_residents] == ["b"]
    # The pool is 30 units away, out of sight, so not yet known.
    assert seen.known_pools == ()
    assert them.my_offers[0].to_id == "a"
    assert them.me.tokens == 95 and them.me.tokens_in_offers == 5
    assert [pool.id for pool in them.known_pools] == ["p1"]
    assert them.known_pools[0].walk_cost == 10


def test_observation_reports_how_long_earlier_rounds_took() -> None:
    game, _ = make_game()
    perception = game.begin_turn("a")

    first = observe(game.world, "a", perception, game.remaining())
    later = observe(game.world, "a", perception, game.remaining(), (30.0, 90.0))

    assert (first.last_thinking_seconds, first.avg_thinking_seconds) == (None, None)
    assert (later.last_thinking_seconds, later.avg_thinking_seconds) == (90.0, 60.0)
    assert json.loads(to_json(later))["avg_thinking_seconds"] == 60.0


def rig_world() -> World:
    """A rig with one MK1 slot on p1, which is in sight of "b" but not of "a"."""
    miner = Resident("c", "丙", "#778899", Position(80, 50), Inventory((("MK1", 1),)), coins=10000)
    world = replace(WORLD, residents=(*WORLD.residents, miner))
    return perform(perform(world, BuildRig("c", "p1")), InstallChip("c", 1, "MK1", 500))


def test_pool_in_sight_shows_how_fast_rigs_draw_it_down() -> None:
    game, _ = make_game(rig_world())

    seen = look(game, "b").known_pools[0]

    assert seen.in_sight and seen.seen_ago == 0
    assert seen.rig_draw_per_second == 3  # 60 tokens every 20 seconds.
    assert seen.seconds_to_empty == 100  # 300 tokens at 3 a second.


def test_pool_out_of_sight_keeps_its_old_reserve_and_hides_the_draw() -> None:
    game, clock = make_game(rig_world())
    look(game, "b")
    game.act(MoveTo("b", Position(50, 50)))
    clock.now = 45  # Two rig cycles of 60 tokens have passed.

    seen = look(game, "b").known_pools[0]

    assert game.world.pools[0].reserve == 180
    assert not seen.in_sight and seen.seen_ago == 45
    assert seen.reserve == 300
    assert seen.rig_draw_per_second is None and seen.seconds_to_empty is None


def test_rigs_on_a_pool_never_seen_are_not_listed() -> None:
    game, _ = make_game(rig_world())

    unaware = look(game, "a")
    aware = look(game, "b")

    assert unaware.known_pools == () and unaware.mining_rigs == ()
    assert [rig["pool_id"] for rig in aware.mining_rigs] == ["p1"]


def shared_rig_world() -> World:
    """Owner "c" and tenant "b" each have a slot in rig 1 on p1; "a" knows nothing of it."""
    stock = Inventory((("MK1", 1), (TOKEN, 100)))
    at_pool = Position(80, 50)
    residents = (
        WORLD.residents[0],
        Resident("b", "乙", "#445566", at_pool, stock, coins=10000),
        Resident("c", "丙", "#778899", at_pool, stock, coins=10000),
    )
    world = perform(replace(WORLD, residents=residents), BuildRig("c", "p1"))
    return perform(perform(world, InstallChip("c", 1, "MK1", 500)), InstallChip("b", 1, "MK1", 500))


def slot_holders(seen: Observation) -> list[list[object]]:
    return [[slot["resident_id"] for slot in rig["slots"]] for rig in seen.mining_rigs]  # type: ignore[attr-defined]


def test_an_owner_away_from_the_pool_still_sees_every_slot_of_their_rig() -> None:
    game, clock = make_game(shared_rig_world())
    game.act(MoveTo("c", Position(50, 50)))
    clock.now = 20

    owner = look(game, "c")

    assert not owner.known_pools or owner.known_pools[0].in_sight is False
    assert slot_holders(owner) == [["c", "b"]]


def test_a_tenant_away_from_the_pool_sees_only_their_own_slot() -> None:
    game, clock = make_game(shared_rig_world())
    game.act(MoveTo("b", Position(50, 50)))
    clock.now = 20

    tenant = look(game, "b")

    assert tenant.known_pools[0].in_sight is False
    assert slot_holders(tenant) == [["b"]]


def test_a_neighbour_mining_at_a_pool_never_seen_does_not_name_it() -> None:
    watcher = replace(WORLD.residents[0], position=Position(58, 50))  # 22 from p1.
    miner = Resident("c", "丙", "#778899", Position(76.5, 50), Inventory((("MK1", 1),)), coins=9000)
    game, _ = make_game(replace(WORLD, residents=(watcher, WORLD.residents[1], miner)))
    game.act(Mine("c", "p1"))

    seen = look(game, "a")

    assert seen.known_pools == ()
    assert [(r.id, r.mining_pool) for r in seen.nearby_residents if r.id == "c"] == [("c", None)]
    assert [(h.kind, h.pool_id) for h in seen.activity] == [("started_mining", None)]


def test_pool_without_rigs_has_no_time_to_empty() -> None:
    game, _ = make_game()

    seen = look(game, "b").known_pools[0]

    assert seen.rig_draw_per_second == 0 and seen.seconds_to_empty is None


def test_observation_json_is_compact_and_rounded() -> None:
    game, clock = make_game()

    clock.now = 1 / 3
    shown = json.loads(to_json(look(game, "a")))

    assert shown["time"] == 0.3
    assert shown["me"]["position"] == {"x": 50, "y": 50}
