"""How the town's economy went: a report computed from the coin ledger and the world.

The report answers whether residents make their money from each other or from the system,
see the economy metrics design in the client repository (`docs/design/economy-metrics.md`).
The headline is `resident_share`: coins residents paid each other, out of those plus the
coins the system paid residents. The server writes the report when a game ends, see
`write_economy_report`; it can be computed for any world.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core.ledger import SYSTEM, Transfer
from ..core.world import World
from .lifecycle import item_value, score, total_coins


@dataclass(frozen=True)
class ChannelTotal:
    transfers: int
    coins: int


@dataclass(frozen=True)
class PriceStats:
    """Unit prices paid for one item kind through one channel."""

    trades: int
    quantity: int
    mean: float
    """Coins per item over all trades, so big trades weigh more."""
    low: float
    high: float
    stdev: float
    """Spread of the per-trade unit prices."""


@dataclass(frozen=True)
class ContractStats:
    signed: int
    deliveries: int
    """Deliveries paid for under contracts."""
    forfeits: int
    forfeited_coins: int
    ended: dict[str, int]
    """Ended contracts by reason. The world keeps only the latest ones, so on a long game
    this can count fewer than were signed."""


@dataclass(frozen=True)
class ResidentEconomy:
    id: str
    name: str
    total_coins: int
    """Coins including those set aside, see `lifecycle.total_coins`."""
    item_value: int
    """What energy and crystals left over count for, see `lifecycle.item_value`."""
    score: int
    """What the ranking counts: `total_coins` plus `item_value`."""
    income: dict[str, int]
    """Coins received, by channel."""
    spending: dict[str, int]
    """Coins paid, by channel."""
    income_concentration: float | None
    """Herfindahl index of `income` over channels: 1 when all income came one way, lower
    the more ways it came; None without income."""


@dataclass(frozen=True)
class EconomyReport:
    time: float
    """Game time the report was made at."""
    between_residents: int
    """Coins residents paid each other."""
    from_system: int
    """Coins the system paid residents."""
    to_system: int
    """Coins residents paid the system."""
    resident_share: float | None
    """`between_residents` out of it plus `from_system`; None when no coins moved."""
    channels: dict[str, ChannelTotal]
    prices: dict[str, dict[str, PriceStats]]
    """Channel to item kind to the unit prices paid."""
    contracts: ContractStats
    coin_gini: float
    """Inequality of the residents' total coins, 0 when all are equal."""
    mean_income_concentration: float | None
    """Average `income_concentration` over residents with income; higher means more
    specialised."""
    residents: tuple[ResidentEconomy, ...]
    transfers_dropped: int
    """Transfers the ledger no longer kept; above 0 the coin figures are partial."""


def economy_report(world: World) -> EconomyReport:
    transfers = world.ledger.transfers
    between = sum(t.amount for t in transfers if SYSTEM not in (t.payer, t.payee))
    from_system = sum(t.amount for t in transfers if t.payer == SYSTEM)
    moved = between + from_system
    residents = tuple(_resident(world, resident.id, resident.name) for resident in world.residents)
    concentrations = [
        r.income_concentration for r in residents if r.income_concentration is not None
    ]
    return EconomyReport(
        time=world.time,
        between_residents=between,
        from_system=from_system,
        to_system=sum(t.amount for t in transfers if t.payee == SYSTEM),
        resident_share=between / moved if moved else None,
        channels=_channels(transfers),
        prices=_prices(transfers),
        contracts=_contracts(world),
        coin_gini=_gini([r.total_coins for r in residents]),
        mean_income_concentration=(
            sum(concentrations) / len(concentrations) if concentrations else None
        ),
        residents=residents,
        transfers_dropped=world.ledger.dropped,
    )


def report_to_json(report: EconomyReport) -> str:
    return json.dumps(asdict(report), ensure_ascii=False, indent=2, allow_nan=False)


def format_report(report: EconomyReport) -> str:
    """A few lines for the console."""
    share = "-" if report.resident_share is None else f"{report.resident_share:.1%}"
    lines = [
        f"economy at {report.time:.0f}s: between residents {report.between_residents}, "
        f"from system {report.from_system}, to system {report.to_system}, "
        f"resident share {share}",
    ]
    for channel, total in sorted(report.channels.items()):
        lines.append(f"  {channel:<17} {total.transfers:>5} transfers {total.coins:>9} coins")
    contracts = report.contracts
    lines.append(
        f"  contracts: {contracts.signed} signed, {contracts.deliveries} deliveries, "
        f"{contracts.forfeits} forfeits"
    )
    concentration = report.mean_income_concentration
    lines.append(
        f"  coin gini {report.coin_gini:.3f}, income concentration "
        + ("-" if concentration is None else f"{concentration:.3f}")
    )
    lines.append(
        "  scores: "
        + ", ".join(
            f"{r.name} {r.score} ({r.total_coins} + {r.item_value})" for r in report.residents
        )
    )
    if report.transfers_dropped:
        lines.append(f"  partial: {report.transfers_dropped} older transfers were dropped")
    return "\n".join(lines)


def write_economy_report(path: Path, world: World) -> EconomyReport:
    """Write the world's report to `path` as JSON, replacing any earlier one, and return it."""
    report = economy_report(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_to_json(report) + "\n", encoding="utf-8")
    return report


def _channels(transfers: tuple[Transfer, ...]) -> dict[str, ChannelTotal]:
    counts: Counter[str] = Counter()
    coins: Counter[str] = Counter()
    for transfer in transfers:
        counts[transfer.channel] += 1
        coins[transfer.channel] += transfer.amount
    return {channel: ChannelTotal(counts[channel], coins[channel]) for channel in sorted(counts)}


def _prices(transfers: tuple[Transfer, ...]) -> dict[str, dict[str, PriceStats]]:
    groups: dict[str, dict[str, list[Transfer]]] = {}
    for transfer in transfers:
        if transfer.item is not None:
            groups.setdefault(transfer.channel, {}).setdefault(transfer.item, []).append(transfer)
    return {
        channel: {item: _price_stats(trades) for item, trades in sorted(items.items())}
        for channel, items in sorted(groups.items())
    }


def _price_stats(trades: list[Transfer]) -> PriceStats:
    units = [t.amount / t.quantity for t in trades]
    quantity = sum(t.quantity for t in trades)
    average = sum(units) / len(units)
    return PriceStats(
        trades=len(trades),
        quantity=quantity,
        mean=sum(t.amount for t in trades) / quantity,
        low=min(units),
        high=max(units),
        stdev=math.sqrt(sum((unit - average) ** 2 for unit in units) / len(units)),
    )


def _contracts(world: World) -> ContractStats:
    transfers = world.ledger.transfers
    forfeits = [t for t in transfers if t.channel == "contract_forfeit"]
    return ContractStats(
        signed=world.contracts.next_id - 1,
        deliveries=sum(t.channel == "contract_payment" for t in transfers),
        forfeits=len(forfeits),
        forfeited_coins=sum(t.amount for t in forfeits),
        ended=dict(sorted(Counter(end.reason for end in world.contracts.ended).items())),
    )


def _resident(world: World, resident_id: str, name: str) -> ResidentEconomy:
    income: Counter[str] = Counter()
    spending: Counter[str] = Counter()
    for transfer in world.ledger.transfers:
        if transfer.payee == resident_id:
            income[transfer.channel] += transfer.amount
        if transfer.payer == resident_id:
            spending[transfer.channel] += transfer.amount
    earned = sum(income.values())
    return ResidentEconomy(
        id=resident_id,
        name=name,
        total_coins=total_coins(world, resident_id),
        item_value=item_value(world, resident_id),
        score=score(world, resident_id),
        income=dict(sorted(income.items())),
        spending=dict(sorted(spending.items())),
        income_concentration=(
            sum((coins / earned) ** 2 for coins in income.values()) if earned else None
        ),
    )


def _gini(values: list[int]) -> float:
    total = sum(values)
    if not values or total == 0:
        return 0.0
    differences = sum(abs(a - b) for a in values for b in values)
    return differences / (2 * len(values) * total)
