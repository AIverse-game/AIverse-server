from dataclasses import replace

import pytest

from aiverse_server.core.ledger import LEDGER_LIMIT, SYSTEM, Ledger, Transfer, record_transfer
from aiverse_server.core.world import Position, Resident, World, WorldMap

WORLD = World(WorldMap(10), (Resident("a", "a", "#123456", Position(1, 1), coins=100),))


def test_a_transfer_is_noted_at_the_current_game_time() -> None:
    world = record_transfer(replace(WORLD, time=12.5), SYSTEM, "a", 40, "exchange", "poster", 2)

    assert world.ledger == Ledger((Transfer(12.5, SYSTEM, "a", 40, "exchange", "poster", 2),))


def test_noting_a_transfer_moves_no_coins() -> None:
    world = record_transfer(WORLD, SYSTEM, "a", 40, "land")

    assert world.residents == WORLD.residents


def test_a_full_ledger_drops_the_oldest_transfer_and_counts_it() -> None:
    full = Ledger(tuple(Transfer(0, SYSTEM, "a", n + 1, "land") for n in range(LEDGER_LIMIT)))

    world = record_transfer(replace(WORLD, ledger=full), "a", SYSTEM, 7, "land")

    assert len(world.ledger.transfers) == LEDGER_LIMIT
    assert world.ledger.transfers[0].amount == 2
    assert world.ledger.transfers[-1].amount == 7
    assert world.ledger.dropped == 1


@pytest.mark.parametrize(
    ("payer", "payee", "amount", "item", "quantity"),
    [
        (SYSTEM, "a", 0, None, 0),
        (SYSTEM, "a", True, None, 0),
        ("a", "a", 5, None, 0),
        (SYSTEM, "a", 5, "poster", 0),
        (SYSTEM, "a", 5, None, 3),
    ],
)
def test_impossible_transfers_are_refused(
    payer: str, payee: str, amount: int, item: str | None, quantity: int
) -> None:
    with pytest.raises(ValueError):
        Transfer(0, payer, payee, amount, "land", item, quantity)
