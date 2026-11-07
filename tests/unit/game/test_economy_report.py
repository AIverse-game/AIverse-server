import json
import math
from pathlib import Path

import pytest

from aiverse_server.core.ledger import SYSTEM, Ledger, Transfer
from aiverse_server.core.world import (
    Contract,
    ContractBook,
    ContractEnd,
    Position,
    Resident,
    World,
    WorldMap,
)
from aiverse_server.economy.compute import COMPUTE
from aiverse_server.game.economy_report import (
    economy_report,
    format_report,
    report_to_json,
    write_economy_report,
)


def resident(id: str, coins: int) -> Resident:
    return Resident(id, id.upper(), "#123456", Position(1, 1), coins=coins)


# a harvests and sells to the market, b buys compute from a and rents a's collector, c does nothing.
TRANSFERS = (
    Transfer(10, SYSTEM, "a", 600, "market", COMPUTE, 30),
    Transfer(20, "b", "a", 300, "offer", COMPUTE, 20),
    Transfer(30, "b", "a", 100, "offer", COMPUTE, 5),
    Transfer(40, "b", "a", 30, "rent"),
    Transfer(40, "b", SYSTEM, 12, "upkeep"),
    Transfer(50, "a", SYSTEM, 300, "program_import", "v1", 1),
)
WORLD = World(
    WorldMap(10),
    (resident("a", 3000), resident("b", 1000), resident("c", 0)),
    time=60,
    ledger=Ledger(TRANSFERS),
)


def test_the_resident_share_counts_coins_residents_paid_each_other() -> None:
    report = economy_report(WORLD)

    assert (report.between_residents, report.from_system, report.to_system) == (430, 600, 312)
    assert report.resident_share == pytest.approx(430 / 1030)


def test_channels_total_their_transfers_and_coins() -> None:
    channels = economy_report(WORLD).channels

    assert {name: (t.transfers, t.coins) for name, t in channels.items()} == {
        "program_import": (1, 300),
        "upkeep": (1, 12),
        "market": (1, 600),
        "offer": (2, 400),
        "rent": (1, 30),
    }


def test_prices_weigh_each_trade_by_its_quantity() -> None:
    offer = economy_report(WORLD).prices["offer"][COMPUTE]

    # 300 for 20 is 15 each and 100 for 5 is 20 each: 400 coins for 25 compute is 16.
    assert (offer.trades, offer.quantity, offer.mean) == (2, 25, 16)
    assert (offer.low, offer.high, offer.stdev) == (15, 20, 2.5)


def test_each_resident_has_income_and_spending_by_channel() -> None:
    a, b, c = economy_report(WORLD).residents

    assert a.income == {"market": 600, "offer": 400, "rent": 30}
    assert a.spending == {"program_import": 300}
    assert b.spending == {"upkeep": 12, "offer": 400, "rent": 30}
    assert a.income_concentration == pytest.approx((600**2 + 400**2 + 30**2) / 1030**2)
    assert (b.income_concentration, c.income_concentration) == (None, None)


def test_the_mean_concentration_is_over_residents_with_income() -> None:
    report = economy_report(WORLD)

    assert report.mean_income_concentration == report.residents[0].income_concentration


def test_the_coin_gini_is_zero_when_all_are_equal_and_grows_with_inequality() -> None:
    equal = World(WorldMap(10), (resident("a", 5), resident("b", 5)))

    # Pairwise differences 2000, 3000, 1000, both ways, over 2 x 3 residents x 4000 coins.
    assert economy_report(equal).coin_gini == 0
    assert economy_report(WORLD).coin_gini == pytest.approx(12_000 / 24_000)


def test_no_coins_moved_gives_no_share_and_no_concentration() -> None:
    report = economy_report(World(WorldMap(10), (resident("a", 0),)))

    assert report.resident_share is None
    assert report.mean_income_concentration is None
    assert report.coin_gini == 0


def test_contracts_count_signings_deliveries_forfeits_and_endings() -> None:
    contract = Contract(1, "a", "b", COMPUTE, 10, 200, 1, 0, 60, deposit=50)
    world = World(
        WorldMap(10),
        (resident("a", 0), resident("b", 0)),
        time=60,
        contracts=ContractBook(next_id=3, ended=(ContractEnd(contract, 60, "lapsed", None, 50),)),
        ledger=Ledger(
            (
                Transfer(10, "b", "a", 200, "contract_payment", COMPUTE, 10),
                Transfer(60, "a", "b", 50, "contract_forfeit"),
            )
        ),
    )

    contracts = economy_report(world).contracts

    assert (contracts.signed, contracts.deliveries) == (2, 1)
    assert (contracts.forfeits, contracts.forfeited_coins) == (1, 50)
    assert contracts.ended == {"lapsed": 1}


def test_the_report_writes_as_json_and_reads_back_its_figures(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "summary.json"
    path.parent.mkdir()
    path.write_text("an earlier game", encoding="utf-8")

    report = write_economy_report(path, WORLD)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == json.loads(report_to_json(report))
    assert data["between_residents"] == 430
    assert math.isclose(data["resident_share"], 430 / 1030)
    assert data["residents"][0]["income"]["rent"] == 30


def test_the_console_summary_names_the_share_and_a_partial_ledger() -> None:
    partial = World(WorldMap(10), WORLD.residents, ledger=Ledger(TRANSFERS, dropped=4))

    text = format_report(economy_report(partial))

    assert "resident share 41.7%" in text
    assert "4 older transfers were dropped" in text
