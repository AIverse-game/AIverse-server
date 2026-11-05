import json
from dataclasses import replace

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.core.ledger import SYSTEM, Ledger, Transfer
from aiverse_server.core.orders import Fill, OrderBook
from aiverse_server.core.world import (
    Inventory,
    Market,
    Position,
    PricePoint,
    Resident,
    Spring,
    World,
    WorldMap,
)
from aiverse_server.economy.energy import ENERGY
from aiverse_server.game.actions import (
    BuildTower,
    Harvest,
    InstallCrystal,
    MoveTo,
    OfferToSell,
    SellEnergy,
    perform,
)
from aiverse_server.residents.perception import begin_turn
from aiverse_server.server.app import Game

WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((ENERGY, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((ENERGY, 100),)), coins=5000),
    ),
    springs=(Spring("p1", Position(80, 50), 4, 300),),
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


def test_observation_shows_self_market_springs_offers_and_ranking() -> None:
    game, _ = make_game()
    game.act(OfferToSell("b", "a", ENERGY, 5, 80))

    seen = look(game, "a")
    them = look(game, "b")

    assert seen.me.at_market and seen.me.energy == 100 and seen.me.coins == 5000
    assert seen.market is not None and seen.market.distance == 0
    assert seen.offers_to_me[0].from_id == "b" and seen.offers_to_me[0].side == "sell"
    assert [standing.id for standing in seen.residents] == ["a", "b"]
    assert [resident.id for resident in seen.nearby_residents] == ["b"]
    # The spring is 30 units away, out of sight, so not yet known.
    assert seen.known_springs == ()
    assert them.my_offers[0].to_id == "a"
    assert them.me.energy == 95 and them.me.energy_in_offers == 5
    assert [spring.id for spring in them.known_springs] == ["p1"]
    assert them.known_springs[0].walk_cost == 10


def test_observation_reports_how_long_earlier_rounds_took() -> None:
    game, _ = make_game()
    perception = game.begin_turn("a")

    first = observe(game.world, "a", perception, game.remaining())
    later = observe(game.world, "a", perception, game.remaining(), (30.0, 90.0))

    assert (first.last_thinking_seconds, first.avg_thinking_seconds) == (None, None)
    assert (later.last_thinking_seconds, later.avg_thinking_seconds) == (90.0, 60.0)
    assert json.loads(to_json(later))["avg_thinking_seconds"] == 60.0


def test_market_trend_shows_price_and_anonymous_sales_over_time() -> None:
    assert WORLD.market is not None
    market = replace(WORLD.market, history=(PricePoint(0.0, 20.0),))
    game, clock = make_game(replace(WORLD, market=market))
    game.act(SellEnergy("a", 20))
    clock.now = 40.0

    seen = look(game, "b")
    clock.now = 360.0
    later = look(game, "b")

    assert seen.market is not None
    assert seen.market.trend[0].sold == 20
    assert seen.market.trend[-1].time == 40.0
    earlier_price = seen.market.trend[0].price
    later_price = seen.market.trend[-1].price
    assert earlier_price is not None and later_price is not None
    assert earlier_price < later_price
    assert later.market is not None
    assert len(later.market.trend) <= 11
    assert all(point.time >= 60.0 for point in later.market.trend)


def test_market_trend_keeps_sales_when_old_price_samples_are_evicted() -> None:
    assert WORLD.market is not None
    world = replace(
        WORLD,
        time=350.0,
        market=replace(WORLD.market, history=(PricePoint(350.0, 20.0),)),
        ledger=Ledger((Transfer(90.0, SYSTEM, "a", 100, "market", ENERGY, 5),)),
    )
    world, perception = begin_turn(world, "b")

    seen = observe(world, "b", perception, 250.0)

    assert seen.market is not None
    assert seen.market.trend[0].sold == 5
    assert seen.market.trend[0].price is None
    assert seen.market.trend[0].time == 120.0
    assert seen.market.trend[-1].price is not None
    assert json.loads(to_json(seen))["market"]["trend"][0]["price"] is None


def test_exchange_history_is_bounded_and_does_not_reveal_parties() -> None:
    fills = tuple(Fill(i, float(i * 40), "C1", 1, 500 + i, "a", "b", "a") for i in range(1, 12))
    game, clock = make_game(replace(WORLD, orders=OrderBook(fills=fills, next_seq=12)))
    clock.now = 440.0

    seen = look(game, "a")
    public_trades = json.loads(to_json(seen))["exchange"]["recent_trades"]

    assert len(public_trades) == 8
    assert public_trades[0]["price"] == 504
    assert public_trades[-1]["ago"] == 0
    assert all("buyer_id" not in trade and "seller_id" not in trade for trade in public_trades)


def tower_world() -> World:
    """A tower with one C1 slot on p1, which is in sight of "b" but not of "a"."""
    harvester = Resident(
        "c", "丙", "#778899", Position(80, 50), Inventory((("C1", 1),)), coins=10000
    )
    world = replace(WORLD, residents=(*WORLD.residents, harvester))
    return perform(perform(world, BuildTower("c", "p1")), InstallCrystal("c", 1, "C1", 500))


def test_spring_in_sight_shows_how_fast_towers_draw_it_down() -> None:
    game, _ = make_game(tower_world())

    seen = look(game, "b").known_springs[0]

    assert seen.in_sight and seen.seen_ago == 0
    assert seen.tower_draw_per_second == 3  # 60 energy every 20 seconds.
    assert seen.seconds_to_empty == 100  # 300 energy at 3 a second.


def test_spring_out_of_sight_keeps_its_old_reserve_and_hides_the_draw() -> None:
    game, clock = make_game(tower_world())
    look(game, "b")
    game.act(MoveTo("b", Position(50, 50)))
    clock.now = 45  # Two tower cycles of 60 energy have passed.

    seen = look(game, "b").known_springs[0]

    assert game.world.springs[0].reserve == 180
    assert not seen.in_sight and seen.seen_ago == 45
    assert seen.reserve == 300
    assert seen.tower_draw_per_second is None and seen.seconds_to_empty is None


def test_spring_reserve_trend_uses_only_reserves_the_resident_saw() -> None:
    game, clock = make_game(tower_world())
    look(game, "b")
    clock.now = 65.0
    second = look(game, "b")
    game.act(MoveTo("b", Position(50, 50)))
    clock.now = 110.0

    away = look(game, "b")

    assert [sample.reserve for sample in second.known_springs[0].reserve_history] == [300, 120]
    assert game.world.springs[0].reserve == 0
    assert [sample.reserve for sample in away.known_springs[0].reserve_history] == [300, 120]


def test_towers_on_a_spring_never_seen_are_not_listed() -> None:
    game, _ = make_game(tower_world())

    unaware = look(game, "a")
    aware = look(game, "b")

    assert unaware.known_springs == () and unaware.towers == ()
    assert [tower["spring_id"] for tower in aware.towers] == ["p1"]


def shared_tower_world() -> World:
    """Owner "c" and tenant "b" each have a slot in tower 1 on p1; "a" knows nothing of it."""
    stock = Inventory((("C1", 1), (ENERGY, 100)))
    at_spring = Position(80, 50)
    residents = (
        WORLD.residents[0],
        Resident("b", "乙", "#445566", at_spring, stock, coins=10000),
        Resident("c", "丙", "#778899", at_spring, stock, coins=10000),
    )
    world = perform(replace(WORLD, residents=residents), BuildTower("c", "p1"))
    return perform(
        perform(world, InstallCrystal("c", 1, "C1", 500)), InstallCrystal("b", 1, "C1", 500)
    )


def slot_holders(seen: Observation) -> list[list[object]]:
    return [[slot["resident_id"] for slot in tower["slots"]] for tower in seen.towers]  # type: ignore[attr-defined]


def test_an_owner_away_from_the_spring_still_sees_every_slot_of_their_tower() -> None:
    game, clock = make_game(shared_tower_world())
    game.act(MoveTo("c", Position(50, 50)))
    clock.now = 20

    owner = look(game, "c")

    assert not owner.known_springs or owner.known_springs[0].in_sight is False
    assert slot_holders(owner) == [["c", "b"]]


def test_a_tenant_away_from_the_spring_sees_only_their_own_slot() -> None:
    game, clock = make_game(shared_tower_world())
    game.act(MoveTo("b", Position(50, 50)))
    clock.now = 20

    tenant = look(game, "b")

    assert tenant.known_springs[0].in_sight is False
    assert slot_holders(tenant) == [["b"]]


def test_a_neighbour_harvesting_at_a_spring_never_seen_does_not_name_it() -> None:
    watcher = replace(WORLD.residents[0], position=Position(58, 50))  # 22 from p1.
    harvester = Resident(
        "c", "丙", "#778899", Position(76.5, 50), Inventory((("C1", 1),)), coins=9000
    )
    game, _ = make_game(replace(WORLD, residents=(watcher, WORLD.residents[1], harvester)))
    game.act(Harvest("c", "p1"))

    seen = look(game, "a")

    assert seen.known_springs == ()
    assert [(r.id, r.harvesting_spring) for r in seen.nearby_residents if r.id == "c"] == [
        ("c", None)
    ]
    assert [(h.kind, h.spring_id) for h in seen.activity] == [("started_harvesting", None)]


def test_spring_without_towers_has_no_time_to_empty() -> None:
    game, _ = make_game()

    seen = look(game, "b").known_springs[0]

    assert seen.tower_draw_per_second == 0 and seen.seconds_to_empty is None


def test_observation_json_is_compact_and_rounded() -> None:
    game, clock = make_game()

    clock.now = 1 / 3
    shown = json.loads(to_json(look(game, "a")))

    assert shown["time"] == 0.3
    assert shown["me"]["position"] == {"x": 50, "y": 50}
