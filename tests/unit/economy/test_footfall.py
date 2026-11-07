from dataclasses import replace

from aiverse_server.core.world import Position, Resident, World, WorldMap
from aiverse_server.economy.footfall import footfall, visitors
from aiverse_server.game.actions import MoveTo, perform


def resident(resident_id: str, x: float, y: float) -> Resident:
    return Resident(resident_id, resident_id, "#123456", Position(x, y))


# The centre of this 100-wide map is (50, 50).
EMPTY = World(map=WorldMap(size=100), residents=())


def test_visitors_thin_out_away_from_the_centre_down_to_a_quarter() -> None:
    distances = (0, 37.5, 75, 90)

    shares = [footfall(EMPTY, Position(50, 50 + distance)) for distance in distances]

    assert shares == [100, 62.5, 25, 25]


def test_visitors_grow_as_the_game_goes_on() -> None:
    assert [visitors(0), visitors(600), visitors(1_200)] == [100, 150, 200]


def test_footfall_follows_the_game_time() -> None:
    world = replace(EMPTY, time=1_200.0)

    assert footfall(world, Position(50, 50)) == 200


def test_residents_standing_by_add_to_the_footfall_but_walkers_do_not() -> None:
    standing = World(
        map=WorldMap(size=100), residents=(resident("a", 50, 40), resident("b", 95, 95))
    )
    walking = perform(standing, MoveTo("a", Position(95, 95)))
    spot = Position(50, 45)

    assert footfall(standing, spot) == 100 * (1 - 0.75 * 5 / 75) + 10
    assert footfall(walking, spot) == 100 * (1 - 0.75 * 5 / 75)


def test_residents_who_left_do_not_add_to_the_footfall() -> None:
    gone = World(map=WorldMap(size=100), residents=(replace(resident("a", 50, 40), left=True),))

    assert footfall(gone, Position(50, 50)) == 100
