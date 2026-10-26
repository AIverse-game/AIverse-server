"""The fixed starting layout. Every match currently uses this same map."""

from .world import Position, Resident, World, WorldMap

MAP_SIZE = 100.0

# Demo residents; spread out so the coordinate system is easy to check on screen.
DEFAULT_RESIDENTS = (
    Resident(id="r1", name="阿青", color="#e4572e", position=Position(20, 25)),
    Resident(id="r2", name="小麦", color="#f3a712", position=Position(72, 18)),
    Resident(id="r3", name="松木", color="#5fb49c", position=Position(35, 70)),
    Resident(id="r4", name="白露", color="#8e9aed", position=Position(82, 84)),
)


def create_default_world() -> World:
    return World(map=WorldMap(size=MAP_SIZE), residents=DEFAULT_RESIDENTS)
