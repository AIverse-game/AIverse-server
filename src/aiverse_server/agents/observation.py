"""What an agent is shown at the start of each round: its perception, put together with what
the resident knows of themselves and what is public.

`observe` adds to the perception (`perception.py`) the resident's own coins, compute, programs
and what they are busy with; the offers and contract proposals waiting for them and those
they sent, the contracts they are party to and the last few of theirs that ended; and every
resident's name, coins and place, which the page shows anyway and which an agent needs to
whisper or trade. Other residents' positions stay limited to those in sight. Distances and
walking costs are worked out from where the resident stands, so a model need not do the
arithmetic. `to_json` turns an observation into compact JSON for a model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from ..core.collectors import Collector, CollectorSlot
from ..core.events import Event, pending_for, pending_from
from ..core.orders import order_coins
from ..core.world import Contract, ContractEnd, Position, PricePoint, SpringSighting, World
from ..economy.collectors import (
    COLLECTOR_CAPACITY,
    COLLECTOR_PRICE,
    COLLECTOR_SECONDS,
    COLLECTOR_YIELD,
    RENT_PER_100,
    UPKEEP_PER_100,
    collector_coins,
    cycle_amount,
    cycle_cost,
    slot_status,
    spring_draw_rate,
)
from ..economy.compute import COMPUTE, walk_cost
from ..economy.contracts import ContractProposal, contract_coins, contracts_of
from ..economy.exchange import EXCHANGE_ITEMS, depth, last_price, volume_since
from ..economy.facilities import (
    AD_MATERIALS,
    AD_RATE,
    AD_SHARE_RADIUS,
    FACILITY_SECONDS,
    ad_incomes,
    visitors,
)
from ..economy.gathering import DATA
from ..economy.land import PARCEL_SIZE, Plot, grid_size, plot, plot_at, plots
from ..economy.market import unit_price
from ..economy.programs import PROGRAM_TIERS
from ..economy.studio import MATERIAL, MATERIAL_RECIPE, RECIPES, write_seconds
from ..economy.trade import PurchaseOffer, SaleOffer, reserved_coins, reserved_items
from ..game.lifecycle import ranks, total_coins
from ..residents.information_offer import InformationOffer
from ..residents.perception import Perception
from ..residents.walking import walk_seconds

ENDED_CONTRACTS_SHOWN = 5
"""Ended contracts an observation lists, the latest."""

BOOK_LEVELS_SHOWN = 3
"""Prices an observation shows on each side of each book, the best."""

VOLUME_WINDOW = 120.0
"""Game seconds over which an observation sums each item's traded units."""

FILLS_SHOWN = 10
"""Fills of the resident's own orders an observation lists, the latest."""
TREND_SECONDS = 300.0
MARKET_TREND_INTERVAL = 30.0
EXCHANGE_TRADES_SHOWN = 8
"""A bounded public history for forecasting without replaying full world snapshots."""


@dataclass(frozen=True)
class Me:
    id: str
    name: str
    position: Position
    coins: int
    """Spendable coins."""
    coins_in_offers: int
    """Coins set aside in purchase offers still waiting for an answer."""
    compute: int
    """Compute carried, which walking and speaking spend."""
    compute_in_offers: int
    coins_in_orders: int
    """Coins set aside in buy orders resting on the exchange."""
    coins_in_contracts: int
    """Coins set aside as a buyer's payments or a supplier's deposits, in contracts in force
    and in proposals not yet answered."""
    programs: dict[str, int]
    """Programs carried, tier to count."""
    data: int
    """Data carried, which writing spends."""
    materials: int
    """Materials carried, which building facilities spends."""
    programs_written: int
    """Programs written so far, which makes writing faster."""
    walking_to: Position | None
    arrives_in: float | None
    """Seconds until the walk ends."""
    harvesting_spring: str | None
    harvesting_done_in: float | None
    """Seconds until the harvesting job pays out."""
    gathering_mine: str | None
    gathering_done_in: float | None
    writing_program: str | None
    """Tier of the program being written, or `material` while making materials."""
    writing_done_in: float | None
    at_market: bool
    at_spring: str | None
    """The spring the resident stands close enough to harvest, if any."""
    at_mine: str | None
    at_studio: bool


@dataclass(frozen=True)
class ProgramOnSale:
    tier: str
    price: int
    compute_per_job: int


@dataclass(frozen=True)
class MarketTrendPoint:
    time: float
    price: float | None
    """Observed market price, or None if its history sample was evicted."""
    sold: int
    """Anonymous compute sold to the market in this 30-second bucket."""


@dataclass(frozen=True)
class MarketView:
    position: Position
    radius: float
    compute_price: float
    """Coins one more compute fetches right now; it falls as compute is sold."""
    distance: float
    walk_cost: int
    """Compute a walk from here to the market's centre costs."""
    walk_seconds: float
    programs: tuple[ProgramOnSale, ...]
    trend: tuple[MarketTrendPoint, ...]
    """At most one price point per 30-second bucket over the last five minutes."""


@dataclass(frozen=True)
class BookView:
    """One item's order book; anonymous, so no one is named."""

    item: str
    bids: tuple[tuple[int, int], ...]
    """The best buy prices with the units wanted at each, highest first."""
    asks: tuple[tuple[int, int], ...]
    """The best sell prices with the units offered at each, lowest first."""
    last_price: int | None
    volume: int
    """Units traded in the last `VOLUME_WINDOW` seconds."""


@dataclass(frozen=True)
class MyOrderView:
    order_id: int
    side: str
    item: str
    quantity: int
    """Units still to fill."""
    price: int
    expires_in: float


@dataclass(frozen=True)
class MyFillView:
    ago: float
    """Seconds since it filled."""
    side: str
    """"bought" or "sold"."""
    item: str
    quantity: int
    price: int


@dataclass(frozen=True)
class ExchangeTradeView:
    ago: float
    item: str
    quantity: int
    price: int
    """An anonymous recent fill, without its buyer or seller."""


@dataclass(frozen=True)
class ExchangeView:
    books: tuple[BookView, ...]
    my_orders: tuple[MyOrderView, ...]
    my_fills: tuple[MyFillView, ...]
    """The latest fills of the resident's orders, oldest first."""
    recent_trades: tuple[ExchangeTradeView, ...]
    """The latest public fills in the past five minutes, oldest first."""


@dataclass(frozen=True)
class MineView:
    """A data mine; mines are public, so the reserve is always current."""

    id: str
    position: Position
    radius: float
    reserve: int
    distance: float
    walk_cost: int
    walk_seconds: float


@dataclass(frozen=True)
class RecipeView:
    tier: str
    data: int
    compute: int
    consumes: str | None
    """Tier of the program it uses up; None for v1."""
    seconds: float
    """Game seconds writing takes this resident, practice counted."""


@dataclass(frozen=True)
class MaterialRecipeView:
    data: int
    compute: int
    count: int
    """Materials one job makes."""
    seconds: float


@dataclass(frozen=True)
class StudioView:
    position: Position
    radius: float
    distance: float
    walk_cost: int
    walk_seconds: float
    recipes: tuple[RecipeView, ...]
    materials: MaterialRecipeView


@dataclass(frozen=True)
class ParcelView:
    """A parcel of land, with its centre to walk to."""

    id: str
    centre: Position
    price: int
    """Coins it costs from the town; what its owner paid if it is owned."""
    reserved: bool
    """It touches a spring, mine, the studio or the market and can never be bought."""
    owner: str | None
    facility: str | None
    """What is built on it, such as `ad`; None when nothing is."""
    ad_income: int | None
    """Coins per cycle an ad slot here earns now: the working one, or one built now. None
    where no ad can be built (reserved, or another's)."""
    distance: float
    walk_cost: int


@dataclass(frozen=True)
class MyParcelView:
    id: str
    centre: Position
    facility: str | None
    last_income: int
    """Coins its latest cycle paid."""
    earned: int
    """Coins it has paid so far."""
    next_payout_in: float | None
    ad_income: int | None
    """Coins per cycle it would earn if paid now, or if an ad were built on bare land."""


@dataclass(frozen=True)
class LandView:
    parcel_size: float
    grid: int
    """Parcels along each side; ids are `L<col>-<row>` from the north-west corner."""
    visitors_now: float
    """Footfall at the market now; it grows as the game goes on."""
    rules: dict[str, int | float]
    here: ParcelView | None
    """The parcel the resident stands in; None while walking."""
    mine: tuple[MyParcelView, ...]
    best_free: tuple[ParcelView, ...]
    """Free parcels where a new ad slot would earn most per cycle now, best first."""


@dataclass(frozen=True)
class SpringView:
    id: str
    position: Position
    radius: float
    reserve: int
    """Compute left when last seen."""
    seen_ago: float
    """Seconds since the reserve was seen; 0 when in sight."""
    in_sight: bool
    """Seen this round, so the reserve is current."""
    collector_draw_per_second: float | None
    """Compute per second the running collectors on this spring take out of it; None unless in
    sight, since a spring out of sight is not watched. Manual harvesting is not counted."""
    seconds_to_empty: float | None
    """Seconds until the reserve is gone at that rate; None unless in sight and collectors are
    drawing on it."""
    distance: float
    walk_cost: int
    walk_seconds: float
    reserve_history: tuple[SpringReserveView, ...]
    """Only reserves this resident actually observed, never unseen updates."""


@dataclass(frozen=True)
class SpringReserveView:
    seen_ago: float
    reserve: int


@dataclass(frozen=True)
class NearbyResident:
    id: str
    name: str
    position: Position
    harvesting_spring: str | None
    walking_to: Position | None


@dataclass(frozen=True)
class Standing:
    """A resident in the public ranking."""

    id: str
    name: str
    coins: int
    """Total coins, including those set aside in offers."""
    rank: int
    left: bool
    programs_written: int


@dataclass(frozen=True)
class OfferView:
    event_id: int
    from_id: str
    to_id: str
    side: str
    """`"buy"` when the sender offers to buy, `"sell"` when the sender offers to sell."""
    kind: str
    quantity: int
    price: int
    """Coins for the whole lot."""


@dataclass(frozen=True)
class InformationOfferView:
    event_id: int
    from_id: str
    to_id: str
    summary: str
    """All the recipient sees until they pay; the content is withheld."""
    price: int
    """Coins for the information."""


@dataclass(frozen=True)
class ProposalView:
    event_id: int
    from_id: str
    to_id: str
    supplier_id: str
    buyer_id: str
    kind: str
    quantity: int
    """Items in each delivery."""
    price: int
    """Coins for each delivery."""
    deliveries: int
    duration_seconds: int
    """How long the contract runs once accepted."""
    deposit: int
    """Coins the supplier stakes on each delivery."""


@dataclass(frozen=True)
class ContractView:
    contract_id: int
    my_role: str
    """`"supplier"` or `"buyer"`."""
    partner_id: str
    kind: str
    quantity: int
    price: int
    deliveries_left: int
    expires_in: float
    escrow: int
    """The buyer's coins set aside for the deliveries left."""
    deposit: int
    """Coins the supplier stakes on each delivery."""
    deposit_held: int
    """The supplier's coins set aside for the deliveries left, the buyer's if not made."""


@dataclass(frozen=True)
class EndedContractView:
    contract_id: int
    my_role: str
    partner_id: str
    kind: str
    quantity: int
    price: int
    reason: str
    """`completed`, `lapsed`, `cancelled` (by the supplier), `party_left` or `game_ended`."""
    deliveries_missed: int
    """Deliveries never made."""
    deposit_forfeited: int
    """The supplier's deposit paid to the buyer for them."""
    ended_ago: float
    """Seconds since it ended."""


@dataclass(frozen=True)
class Line:
    time: float
    speaker_id: str
    text: str
    to: str | None
    """The one resident spoken to; None when said aloud."""


@dataclass(frozen=True)
class NoticeView:
    time: float
    text: str


@dataclass(frozen=True)
class Happening:
    time: float
    resident_id: str
    kind: str
    position: Position
    spring_id: str | None


@dataclass(frozen=True)
class NoteView:
    id: int
    text: str
    expires_in: float | None
    """Seconds until a short-term note is cleared; None for a long-term one."""


@dataclass(frozen=True)
class Observation:
    time: float
    """Game seconds since the start."""
    remaining_seconds: float
    me: Me
    market: MarketView | None
    exchange: ExchangeView
    mines: tuple[MineView, ...]
    """Every mine, nearest first."""
    studio: StudioView | None
    land: LandView
    known_springs: tuple[SpringView, ...]
    """Every spring discovered so far, nearest first."""
    discovered: tuple[str, ...]
    """Springs seen for the first time since the last round: from where the resident stands, or
    on the way there."""
    nearby_residents: tuple[NearbyResident, ...]
    residents: tuple[Standing, ...]
    offers_to_me: tuple[OfferView, ...]
    my_offers: tuple[OfferView, ...]
    information_offers_to_me: tuple[InformationOfferView, ...]
    my_information_offers: tuple[InformationOfferView, ...]
    contract_proposals_to_me: tuple[ProposalView, ...]
    my_contract_proposals: tuple[ProposalView, ...]
    my_contracts: tuple[ContractView, ...]
    ended_contracts: tuple[EndedContractView, ...]
    """The last few contracts of the resident's that ended, oldest first."""
    speech: tuple[Line, ...]
    """What was said in the resident's hearing since they last looked, their own words too."""
    activity: tuple[Happening, ...]
    """What others did nearby since the resident last looked."""
    long_term_notes: tuple[NoteView, ...]
    short_term_notes: tuple[NoteView, ...]
    collector_balance: int
    collector_rules: dict[str, int | float]
    collectors: tuple[dict[str, object], ...]
    notices: tuple[NoticeView, ...] = ()
    """What the server announced to everyone since the resident last looked."""
    last_thinking_seconds: float | None = None
    """Game seconds the resident's previous round took, from its look to the end of the
    round, whether it went well or not; None in the first round."""
    avg_thinking_seconds: float | None = None
    """The same, averaged over all the resident's previous rounds; None in the first round."""


def observe(
    world: World,
    resident_id: str,
    perception: Perception,
    remaining: float,
    thinking: Sequence[float] = (),
) -> Observation:
    """Put the resident's perception together with what they know of themselves and the public
    ranking; `remaining` is the game time left, `thinking` the game seconds each of the
    resident's earlier rounds took, oldest first."""
    me = next(resident for resident in world.residents if resident.id == resident_id)
    here = perception.position
    program_tiers = {program.tier for program in PROGRAM_TIERS}
    walk = me.walking
    job = me.harvesting
    gather = me.gathering
    write = me.writing
    market = world.market
    studio = world.studio
    standings = ranks(world)
    at_spring = next(
        (spring.id for spring in world.springs if spring.reaches(here) and walk is None),
        None,
    )
    in_sight = frozenset(sighting.spring_id for sighting in perception.springs)
    # A neighbour's or a happening's spring is named only if the resident has seen that spring.
    known_ids = {sighting.spring_id for sighting in perception.known_springs}
    springs = sorted(
        (
            _spring_view(
                world,
                sighting,
                here,
                sighting.spring_id in in_sight,
                tuple(
                    SpringReserveView(max(world.time - sample.time, 0.0), sample.reserve)
                    for sample in perception.spring_reserve_history
                    if sample.spring_id == sighting.spring_id
                ),
            )
            for sighting in perception.known_springs
        ),
        key=lambda spring: spring.distance,
    )
    return Observation(
        collector_balance=collector_coins(world, resident_id),
        collector_rules={
            "price": COLLECTOR_PRICE,
            "capacity": COLLECTOR_CAPACITY,
            "cycle_seconds": COLLECTOR_SECONDS,
            "yield_multiplier": COLLECTOR_YIELD,
            "rent_per_100": RENT_PER_100,
            "upkeep_per_100": UPKEEP_PER_100,
        },
        collectors=_collector_views(world, resident_id, in_sight),
        time=perception.time,
        remaining_seconds=remaining,
        last_thinking_seconds=thinking[-1] if thinking else None,
        avg_thinking_seconds=sum(thinking) / len(thinking) if thinking else None,
        me=Me(
            id=me.id,
            name=me.name,
            position=here,
            coins=me.coins,
            coins_in_offers=reserved_coins(world, resident_id),
            compute=me.inventory.count(COMPUTE),
            compute_in_offers=reserved_items(world, resident_id, COMPUTE),
            coins_in_orders=order_coins(world.orders, resident_id),
            coins_in_contracts=contract_coins(world, resident_id),
            programs={
                kind: count
                for kind, count in me.inventory.counts().items()
                if kind in program_tiers
            },
            data=me.inventory.count(DATA),
            materials=me.inventory.count(MATERIAL),
            programs_written=me.programs_written,
            walking_to=walk.target if walk is not None else None,
            arrives_in=max(walk.arrives_at - world.time, 0.0) if walk is not None else None,
            harvesting_spring=job.spring_id if job is not None else None,
            harvesting_done_in=max(job.ends_at - world.time, 0.0) if job is not None else None,
            gathering_mine=gather.mine_id if gather is not None else None,
            gathering_done_in=max(gather.ends_at - world.time, 0.0) if gather is not None else None,
            writing_program=write.tier if write is not None else None,
            writing_done_in=max(write.ends_at - world.time, 0.0) if write is not None else None,
            at_market=market is not None and walk is None and market.reaches(here),
            at_spring=at_spring,
            at_mine=next(
                (mine.id for mine in world.mines if mine.reaches(here) and walk is None),
                None,
            ),
            at_studio=studio is not None and walk is None and studio.reaches(here),
        ),
        market=None
        if perception.market is None
        else MarketView(
            position=perception.market.position,
            radius=perception.market.radius,
            compute_price=perception.market.price,
            distance=_distance(here, perception.market.position),
            walk_cost=walk_cost(here, perception.market.position),
            walk_seconds=walk_seconds(here, perception.market.position),
            programs=tuple(
                ProgramOnSale(program.tier, program.price, program.compute_per_job)
                for program in perception.market.programs
            ),
            trend=_market_trend(world),
        ),
        exchange=_exchange_view(world, resident_id),
        mines=tuple(
            sorted(
                (
                    MineView(
                        mine.id,
                        mine.position,
                        mine.radius,
                        mine.reserve,
                        _distance(here, mine.position),
                        walk_cost(here, mine.position),
                        walk_seconds(here, mine.position),
                    )
                    for mine in world.mines
                ),
                key=lambda mine: mine.distance,
            )
        ),
        studio=None
        if studio is None
        else StudioView(
            studio.position,
            studio.radius,
            _distance(here, studio.position),
            walk_cost(here, studio.position),
            walk_seconds(here, studio.position),
            tuple(
                RecipeView(
                    recipe.tier,
                    recipe.data,
                    recipe.compute,
                    recipe.consumes,
                    write_seconds(recipe, me.programs_written),
                )
                for recipe in RECIPES
            ),
            MaterialRecipeView(
                MATERIAL_RECIPE.data,
                MATERIAL_RECIPE.compute,
                MATERIAL_RECIPE.count,
                MATERIAL_RECIPE.seconds,
            ),
        ),
        land=_land_view(world, resident_id, here, standing=walk is None),
        known_springs=tuple(springs),
        discovered=perception.discovered,
        nearby_residents=tuple(
            NearbyResident(
                id=sighting.resident_id,
                name=sighting.name,
                position=sighting.position,
                harvesting_spring=sighting.harvesting_spring_id
                if sighting.harvesting_spring_id in known_ids
                else None,
                walking_to=sighting.walking_to,
            )
            for sighting in perception.residents
        ),
        residents=tuple(
            Standing(
                id=resident.id,
                name=resident.name,
                coins=total_coins(world, resident.id),
                rank=standings[resident.id],
                left=resident.left,
                programs_written=resident.programs_written,
            )
            for resident in world.residents
        ),
        offers_to_me=_offers(pending_for(world, resident_id)),
        my_offers=_offers(pending_from(world, resident_id)),
        information_offers_to_me=_information_offers(pending_for(world, resident_id)),
        my_information_offers=_information_offers(pending_from(world, resident_id)),
        contract_proposals_to_me=_proposals(pending_for(world, resident_id)),
        my_contract_proposals=_proposals(pending_from(world, resident_id)),
        my_contracts=tuple(
            _contract(contract, resident_id, world.time)
            for contract in contracts_of(world, resident_id)
        ),
        ended_contracts=tuple(
            _ended(end, resident_id, world.time)
            for end in [
                end
                for end in world.contracts.ended
                if resident_id in (end.contract.supplier_id, end.contract.buyer_id)
            ][-ENDED_CONTRACTS_SHOWN:]
        ),
        speech=tuple(
            Line(line.time, line.speaker_id, line.text, line.to) for line in perception.speech
        ),
        notices=tuple(NoticeView(notice.time, notice.text) for notice in perception.notices),
        activity=tuple(
            Happening(
                entry.time,
                entry.resident_id,
                entry.kind,
                entry.position,
                entry.spring_id if entry.spring_id in known_ids else None,
            )
            for entry in perception.activity
        ),
        long_term_notes=tuple(NoteView(note.id, note.text, None) for note in perception.long_term),
        short_term_notes=tuple(
            NoteView(
                note.id,
                note.text,
                None if note.expires_at is None else max(note.expires_at - world.time, 0.0),
            )
            for note in perception.short_term
        ),
    )


LAND_SHOWN = 5
"""Free parcels an observation suggests."""


def _land_view(world: World, resident_id: str, here: Position, *, standing: bool) -> LandView:
    squares = plots(world)
    incomes = ad_incomes(world, squares)
    parcels = {(parcel.col, parcel.row): parcel for parcel in world.parcels}

    def view(square: Plot) -> ParcelView:
        parcel = parcels.get((square.col, square.row))
        facility = parcel.facility.kind if parcel is not None and parcel.facility else None
        buildable = not square.reserved and square.owner_id in (None, resident_id)
        return ParcelView(
            square.id,
            square.centre,
            square.price,
            square.reserved,
            square.owner_id,
            facility,
            incomes[(square.col, square.row)] if buildable or facility else None,
            _distance(here, square.centre),
            walk_cost(here, square.centre),
        )

    free = [square for square in squares if not square.reserved and square.owner_id is None]
    best = sorted(
        free,
        key=lambda square: (
            -incomes[(square.col, square.row)],
            square.price,
            _distance(here, square.centre),
        ),
    )[:LAND_SHOWN]
    mine = tuple(
        MyParcelView(
            parcel.id,
            plot(world, parcel.col, parcel.row).centre,
            parcel.facility.kind if parcel.facility else None,
            parcel.facility.last_income if parcel.facility else 0,
            parcel.facility.earned if parcel.facility else 0,
            max(parcel.facility.next_at - world.time, 0.0) if parcel.facility else None,
            incomes[(parcel.col, parcel.row)],
        )
        for parcel in world.parcels
        if parcel.owner_id == resident_id
    )
    return LandView(
        parcel_size=PARCEL_SIZE,
        grid=grid_size(world),
        visitors_now=visitors(world.time),
        rules={
            "cycle_seconds": FACILITY_SECONDS,
            "ad_materials": AD_MATERIALS,
            "ad_coins_per_footfall": AD_RATE,
            "ad_share_radius": AD_SHARE_RADIUS,
        },
        here=view(plot_at(world, here)) if standing else None,
        mine=mine,
        best_free=tuple(view(square) for square in best),
    )


def _exchange_view(world: World, resident_id: str) -> ExchangeView:
    own_fills = [
        fill for fill in world.orders.fills if resident_id in (fill.buyer_id, fill.seller_id)
    ][-FILLS_SHOWN:]
    return ExchangeView(
        books=tuple(
            BookView(
                item,
                depth(world, item, "buy", BOOK_LEVELS_SHOWN),
                depth(world, item, "sell", BOOK_LEVELS_SHOWN),
                last_price(world, item),
                volume_since(world, item, world.time - VOLUME_WINDOW),
            )
            for item in EXCHANGE_ITEMS
        ),
        my_orders=tuple(
            MyOrderView(
                order.id,
                order.side,
                order.item,
                order.quantity,
                order.price,
                max(order.expires_at - world.time, 0.0),
            )
            for order in world.orders.orders
            if order.resident_id == resident_id
        ),
        my_fills=tuple(
            MyFillView(
                world.time - fill.time,
                "bought" if fill.buyer_id == resident_id else "sold",
                fill.item,
                fill.quantity,
                fill.price,
            )
            for fill in own_fills
        ),
        recent_trades=tuple(
            ExchangeTradeView(world.time - fill.time, fill.item, fill.quantity, fill.price)
            for fill in world.orders.fills
            if world.time - TREND_SECONDS <= fill.time <= world.time
        )[-EXCHANGE_TRADES_SHOWN:],
    )


def _market_trend(world: World) -> tuple[MarketTrendPoint, ...]:
    market = world.market
    if market is None:
        return ()
    cutoff = max(world.time - TREND_SECONDS, 0.0)
    points = {
        int(point.time // MARKET_TREND_INTERVAL): point
        for point in market.history
        if cutoff <= point.time <= world.time
    }
    bucket_now = int(world.time // MARKET_TREND_INTERVAL)
    points[bucket_now] = PricePoint(world.time, unit_price(market, world.time))
    sold: dict[int, int] = {}
    for transfer in world.ledger.transfers:
        if transfer.channel == "market" and cutoff <= transfer.time <= world.time:
            bucket = int(transfer.time // MARKET_TREND_INTERVAL)
            sold[bucket] = sold.get(bucket, 0) + transfer.quantity
    trend = []
    for bucket in sorted(points.keys() | sold.keys()):
        point = points.get(bucket)
        trend.append(
            MarketTrendPoint(
                point.time
                if point is not None
                else min((bucket + 1) * MARKET_TREND_INTERVAL, world.time),
                point.price if point is not None else None,
                sold.get(bucket, 0),
            )
        )
    return tuple(trend)


def to_json(observation: Observation) -> str:
    """Compact JSON, numbers rounded to one decimal place, for a model to read."""
    return json.dumps(_rounded(asdict(observation)), ensure_ascii=False, separators=(",", ":"))


def _offers(events: tuple[Event, ...]) -> tuple[OfferView, ...]:
    """The trade offers among `events`."""
    return tuple(
        OfferView(
            event_id=event.id,
            from_id=event.sender_id,
            to_id=event.recipient_id,
            side="buy" if isinstance(event.payload, PurchaseOffer) else "sell",
            kind=event.payload.kind,
            quantity=event.payload.quantity,
            price=event.payload.price,
        )
        for event in events
        if isinstance(event.payload, PurchaseOffer | SaleOffer)
    )


def _information_offers(events: tuple[Event, ...]) -> tuple[InformationOfferView, ...]:
    """The information offers among `events`, without their content."""
    return tuple(
        InformationOfferView(
            event_id=event.id,
            from_id=event.sender_id,
            to_id=event.recipient_id,
            summary=event.payload.summary.strip(),
            price=event.payload.price,
        )
        for event in events
        if isinstance(event.payload, InformationOffer)
    )


def _proposals(events: tuple[Event, ...]) -> tuple[ProposalView, ...]:
    """The contract proposals among `events`."""
    views = []
    for event in events:
        proposal = event.payload
        if not isinstance(proposal, ContractProposal):
            continue
        supplier_id, buyer_id = proposal.parties(event)
        views.append(
            ProposalView(
                event_id=event.id,
                from_id=event.sender_id,
                to_id=event.recipient_id,
                supplier_id=supplier_id,
                buyer_id=buyer_id,
                kind=proposal.kind,
                quantity=proposal.quantity,
                price=proposal.price,
                deliveries=proposal.deliveries,
                duration_seconds=proposal.duration,
                deposit=proposal.deposit,
            )
        )
    return tuple(views)


def _contract(contract: Contract, resident_id: str, now: float) -> ContractView:
    supplies = contract.supplier_id == resident_id
    return ContractView(
        contract_id=contract.id,
        my_role="supplier" if supplies else "buyer",
        partner_id=contract.buyer_id if supplies else contract.supplier_id,
        kind=contract.kind,
        quantity=contract.quantity,
        price=contract.price,
        deliveries_left=contract.deliveries_left,
        expires_in=max(contract.expires_at - now, 0.0),
        escrow=contract.escrow,
        deposit=contract.deposit,
        deposit_held=contract.deposit_held,
    )


def _ended(end: ContractEnd, resident_id: str, now: float) -> EndedContractView:
    contract = end.contract
    supplies = contract.supplier_id == resident_id
    return EndedContractView(
        contract_id=contract.id,
        my_role="supplier" if supplies else "buyer",
        partner_id=contract.buyer_id if supplies else contract.supplier_id,
        kind=contract.kind,
        quantity=contract.quantity,
        price=contract.price,
        reason=end.reason,
        deliveries_missed=contract.deliveries_left,
        deposit_forfeited=end.forfeit,
        ended_ago=max(now - end.time, 0.0),
    )


def known_slots(
    collector: Collector, resident_id: str, in_sight: frozenset[str]
) -> tuple[CollectorSlot, ...] | None:
    """The slots of a collector the resident can know of: all of them when its spring is in sight
    or the resident owns it, else only their own; None when they know nothing of it. A collector
    on a spring they have not seen would give the spring's state away."""
    if collector.spring_id in in_sight or collector.owner_id == resident_id:
        return collector.slots
    own = tuple(slot for slot in collector.slots if slot.resident_id == resident_id)
    return own or None


def _collector_views(
    world: World, resident_id: str, in_sight: frozenset[str]
) -> tuple[dict[str, object], ...]:
    """The collectors the resident can know of, see `known_slots`."""
    views: list[dict[str, object]] = []
    for collector in world.collectors:
        slots = known_slots(collector, resident_id, in_sight)
        if slots is None:
            continue
        views.append(
            {
                "id": collector.id,
                "owner_id": collector.owner_id,
                "spring_id": collector.spring_id,
                "position": asdict(
                    next(p.position for p in world.springs if p.id == collector.spring_id)
                ),
                "slots": [
                    {
                        "id": s.id,
                        "resident_id": s.resident_id,
                        "program": s.program,
                        "balance": s.balance,
                        "harvested": s.harvested,
                        "rent_paid": s.rent_paid,
                        "status": slot_status(world, collector, s),
                        "next_in": max(s.next_at - world.time, 0.0),
                        "compute_per_cycle": cycle_amount(world, collector, s),
                        "rent_and_upkeep": sum(
                            cycle_cost(collector, s, cycle_amount(world, collector, s))
                        ),
                    }
                    for s in slots
                ],
            }
        )
    return tuple(views)


def _spring_view(
    world: World,
    sighting: SpringSighting,
    here: Position,
    in_sight: bool,
    reserve_history: tuple[SpringReserveView, ...],
) -> SpringView:
    """A known spring; how fast collectors drain it is told only while the resident can see it."""
    draw = spring_draw_rate(world, sighting.spring_id) if in_sight else None
    return SpringView(
        id=sighting.spring_id,
        position=sighting.position,
        radius=sighting.radius,
        reserve=sighting.reserve,
        seen_ago=0.0 if in_sight else max(world.time - sighting.seen_at, 0.0),
        in_sight=in_sight,
        collector_draw_per_second=draw,
        seconds_to_empty=sighting.reserve / draw if draw else None,
        distance=_distance(here, sighting.position),
        walk_cost=walk_cost(here, sighting.position),
        walk_seconds=walk_seconds(here, sighting.position),
        reserve_history=reserve_history,
    )


def _distance(a: Position, b: Position) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))


def _rounded(value: object) -> object:
    if isinstance(value, float):
        return round(value, 1)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_rounded(item) for item in value]
    return value
