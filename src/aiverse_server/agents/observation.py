"""What an agent is shown at the start of each round: its perception, put together with what
the resident knows of themselves and what is public.

`observe` adds to the perception (`perception.py`) the resident's own coins, items and whether
they are walking; their compute, nodes and jobs; the land and the exchange; the offers and
contract proposals waiting for them and those they sent, the contracts they are party to and
the last few of theirs that ended; and every resident's name, coins and place, which the page
shows anyway and which an agent needs to whisper or trade. Other residents' positions stay
limited to those in sight. Walking times are worked out from where the resident stands, so a
model need not do the arithmetic. `to_json`
turns an observation into compact JSON for a model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from ..core.compute import NODE_KINDS, Job, NodeKind, PermitKind
from ..core.events import Event, pending_for, pending_from
from ..core.land import (
    AD_SLOT_CAPACITY,
    MAX_TOLL,
    ORIENTATIONS,
    PARCEL_SHARES,
    Orientation,
    Parcel,
)
from ..core.ledger import SYSTEM
from ..core.orders import order_coins
from ..core.world import Contract, ContractEnd, Position, World
from ..economy.ads import CONTENT_LIFETIME, ITEM_FOR, appeal
from ..economy.commissions import BuildCommission, commission_coins
from ..economy.compute import (
    AD_WORKLOAD,
    GARDEN_WORKLOAD,
    MAKE_JOB,
    MALL_WORKLOAD,
    MAX_NODES,
    NODE_FOR_JOB,
    NODE_SECONDS,
    PORTAL_WORKLOAD,
    SUBSCRIPTION_PRICE,
    VENUE_WORKLOAD,
    garden_room,
    job_seconds,
    making_workload,
    node_supply,
    nodes_free,
    nodes_in_use,
    on_demand_cost,
    pieces_made,
    portal_room,
    practice_factor,
    promised_builder,
)
from ..economy.contracts import ContractProposal, contract_coins, contracts_of
from ..economy.exchange import EXCHANGE_ITEMS, depth, last_price, volume_since
from ..economy.gardens import block_has_garden
from ..economy.joint_purchase import JointPurchase
from ..economy.land import (
    PARCEL_SIZE,
    Plot,
    claimed,
    facility_centre,
    for_sale_at,
    grid_size,
    plot,
    plot_at,
    plots,
    town_centre,
    venue_room,
)
from ..economy.portals import (
    MAX_LINKS_PER_PORTAL,
    MAX_PORTALS,
    MIN_LINK_DISTANCE,
    LinkProposal,
    links_of,
)
from ..economy.shares import ShareOffer, split_income
from ..economy.sponsors import BUDGET_CAP_SECONDS, PAY_PERIOD
from ..economy.trade import PurchaseOffer, SaleOffer, reserved_coins
from ..economy.visitors import Traffic, facilities_built, people_near, visitors_drawn
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
    """Coins set aside in purchase offers and build commissions still waiting for an answer."""
    coins_in_orders: int
    """Coins set aside in buy orders resting on the exchange."""
    coins_in_contracts: int
    """Coins set aside as a buyer's payments or a supplier's deposits, in contracts in force
    and in proposals not yet answered."""
    items: dict[str, int]
    """Items carried, kind to count."""
    walking_to: Position | None
    arrives_in: float | None
    """Seconds until the walk ends."""


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
class RecentPayments:
    """What residents paid each other in the past five minutes, across the whole town: the
    deals that really happened, not the offers. Coins from or to the system, such as land,
    compute and what visitors pay, are not counted."""

    count: int
    coins: int


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
class ParcelView:
    """A parcel of land, with its centre to walk to."""

    id: str
    centre: Position
    price: int
    """Coins it costs from the town; what its owner paid if it is owned."""
    owner: str | None
    people_near: float
    """People passing its centre now, as the scripted visitors happen to be there: it follows the
    layout of the town, and a visitor stands for more people as the game goes on."""
    distance: float
    walk_seconds: float
    facility: str | None = None
    """The kind of facility built on it, such as `ad`; None when it is bare."""
    building_ends_in: float | None = None
    """Seconds until the resident's own job on it finishes; None without one."""
    content: int = 0
    """Pieces of content its ad slot or game hall shows now."""


@dataclass(frozen=True)
class MyParcelView:
    id: str
    centre: Position
    people_near: float
    distance: float
    walk_seconds: float
    facility: str | None = None
    """The kind of facility built on it, such as `ad`; None when it is bare."""
    building_ends_in: float | None = None
    """Seconds until the job building on it finishes; None without one."""
    content_goes_stale_in: tuple[float, ...] = ()
    """Seconds until each piece of content its ad slot or game hall shows goes stale, soonest
    first."""
    income_last_payday: int = 0
    """Coins its ad slot was paid at the last payday that had views to pay for, for the people
    who passed it since the one before; 0 for a slot paid nothing then."""
    shares: dict[str, int] = field(default_factory=dict)
    """Who holds the parcel's shares and how many of the 100; the income is paid out by them."""
    my_cut_last_payday: int = 0
    """The part of `income_last_payday` that went to the resident."""
    people_seen: float = 0.0
    """People the scripted visitors have counted as seeing its ad slot so far this game, see
    `economy/visitors.py`; the sponsors pay for them at paydays, see `LandView.sponsors`."""
    people_admitted: float = 0.0
    """People let into its workspace, game hall or mall so far this game, who paid the fee at the
    door;
    the fees are paid out to the holders at paydays, with the sponsors' pay."""
    covered_by: str | None = None
    """The parcel whose venue, built or being built, takes this one up; nothing else can be built
    here. None when it is not taken up by another parcel's venue."""
    venue_room: bool = False
    """Whether a game hall or workspace could be started with this parcel at its north-west
    corner now: the parcels it would take up are all yours and untaken."""
    promised_to: str | None = None
    """The builder you agreed to let build here, see `build_permits`; nothing else can be started
    on it until the permit is used or lapses. None when it is not promised."""
    mall_orientations: tuple[Orientation, ...] = ()
    """The ways a mall could be started with this parcel at its north-west corner now: north_south
    takes it and the parcel south of it, east_west it and the parcel east of it, in the same block,
    yours and untaken. Empty when it cannot."""
    garden_room: bool = False
    """Whether you could start a garden on this parcel now: yours, bare and untaken, and the
    block has no garden, built or being built."""
    garden_in_block: bool = False
    """Whether a finished garden stands in this parcel's block, which adds a tenth to every
    income of the block's facilities."""
    content_appeal: float = 0.0
    """What the content its ad slot or game hall shows is worth to visitors now, in pieces: an ad
    counts one; a game counts less the longer it has been in the hall, halving every
    `GAME_HALF_LIFE` seconds. A hall with old games draws less than its game count says; a fresh
    game replaces the oldest when the hall is full."""
    portal_room: bool = False
    """Whether a portal could be started on this parcel now, whoever of its holders builds it:
    bare, untaken, not promised, and its owner has fewer than `TransportView.max_portals` portals
    on their parcels, built or being built."""


@dataclass(frozen=True)
class StakeView:
    """A parcel somebody else owns of which the resident holds shares."""

    id: str
    owner: str
    my_shares: int
    shares: dict[str, int]
    """Who holds the parcel's shares and how many of the 100."""
    facility: str | None
    content: int
    """Pieces of content its ad slot shows now."""
    income_last_payday: int
    my_cut_last_payday: int


@dataclass(frozen=True)
class JobView:
    """Work running on one of the resident's sources of compute."""

    job_id: int
    kind: str
    parcel: str | None
    """The parcel an ad slot is being built on; None for content."""
    source: str
    """`self`, `on_demand` or `subscription`."""
    node_id: int | None
    ends_in: float


@dataclass(frozen=True)
class NodeView:
    """A node the resident subscribed to."""

    node_id: int
    kind: str
    expires_in: float
    working_on: int | None
    """The job it is working on; None when idle."""
    free_for: float
    """Seconds until it is free to take a new job: 0 when idle; never more than `expires_in`,
    since a job that outlasts the node carries on without it."""


@dataclass(frozen=True)
class TownNodesView:
    """The town's nodes of one kind. There are only `total` of them for everyone; a subscribed
    node is taken until it runs out, a hired one until its job ends."""

    kind: str
    total: int
    free: int
    """Nodes of this kind nobody has taken now: to subscribe to or hire one, one must be free."""
    held_by: dict[str, int]
    """Residents holding subscribed nodes of this kind, and how many each."""
    hired: int
    """Nodes of this kind taken by hired jobs."""
    next_free_in: float | None
    """Seconds until the soonest taken node of this kind comes back; None when none is taken."""


@dataclass(frozen=True)
class PracticeView:
    """The resident's practice at one kind of content: each piece made makes the next quicker."""

    content: str
    """`ad` or `game`."""
    made: int
    """Pieces of it the resident has made."""
    next_workload_percent: int
    """Percent of the base workload the resident's next piece takes, rounded: 100 for the first,
    less for each piece made, never less than the floor. A whole number, since the observation
    rounds fractions to one place."""


@dataclass(frozen=True)
class AdOption:
    """What a job takes on one source of compute."""

    source: str
    seconds: float
    coins: int
    """Paid now; 0 for your own compute, and for a subscribed node already paid for."""


@dataclass(frozen=True)
class ComputeView:
    """The resident's own compute, nodes and jobs, and what building costs."""

    own_job: JobView | None
    """The job on the resident's own compute; None when it is free."""
    my_nodes: tuple[NodeView, ...]
    my_jobs: tuple[JobView, ...]
    """Every job of the resident's, on any source, soonest first."""
    ad_slot: tuple[AdOption, ...]
    """Time and coins for an ad slot on each source."""
    portal: tuple[AdOption, ...]
    """Time and coins for a portal on each source."""
    venue: tuple[AdOption, ...]
    """Time and coins for a game hall or a workspace on each source."""
    mall: tuple[AdOption, ...]
    """Time and coins for a mall on each source."""
    garden: tuple[AdOption, ...]
    """Time and coins for a garden on each source."""
    content: tuple[AdOption, ...]
    """Time and coins for a piece of ad content on each source."""
    game: tuple[AdOption, ...]
    """Time and coins for a game, the content of a game hall, on each source."""
    node_coins_per_second: int
    node_seconds: tuple[int, int]
    """Least and most seconds one subscription lasts."""
    max_nodes: int
    town_nodes: tuple[TownNodesView, ...]
    """The town's nodes of each kind: how many there are, how many are free and who holds them."""
    practice: tuple[PracticeView, ...]
    """How practised the resident is at each kind of content; `content` and `game` already take
    it into account."""


@dataclass(frozen=True)
class LineView:
    """A line from a portal to another, seen from the portal it starts at."""

    to_parcel: str
    to_owner: str
    to_centre: Position
    toll: int
    """The toll its owners set, 0 to `max_toll`. Residents do not teleport, so nobody pays it;
    visitors pay the system's fixed toll instead."""


@dataclass(frozen=True)
class PortalView:
    parcel: str
    owner: str
    centre: Position
    people_near: float
    """People passing its centre now."""
    lines: tuple[LineView, ...]


@dataclass(frozen=True)
class TransportView:
    """Portals and the lines between them."""

    max_portals: int
    """Portals the parcels of one resident may carry, built or being built by anyone."""
    max_lines_per_portal: int
    min_line_distance: float
    """Least distance between the centres of a line's two parcels."""
    max_toll: int
    here: PortalView | None
    """The portal in the parcel the resident stands in, whoever owns it, with its lines. None
    without one, and while walking."""
    mine: tuple[PortalView, ...]
    others: tuple[PortalView, ...]
    """Portals of other residents, nearest the middle of the map first, a few of them."""


@dataclass(frozen=True)
class LinkProposalView:
    event_id: int
    from_id: str
    to_id: str
    from_parcel: str
    """The proposer's portal."""
    to_parcel: str
    """The recipient's portal."""
    from_centre: Position
    to_centre: Position
    toll: int
    """The toll the proposer set, 0 to `max_toll`. Residents do not teleport, so nobody pays it;
    visitors pay the system's fixed toll instead."""


@dataclass(frozen=True)
class BuildCommissionView:
    """A proposal that someone builds a facility on a parcel for a fee, see `commissions.py`."""

    event_id: int
    from_id: str
    to_id: str
    parcel: str
    centre: Position
    kind: PermitKind
    owner_id: str
    """Whose parcel it is; the parcel's owner pays the fee."""
    builder_id: str
    fee: int
    """Coins the owner pays the builder once the facility is finished. Set aside already when
    the owner sent the proposal, and when the builder did, on acceptance."""
    orientation: Orientation = "north_south"
    """Which way a mall runs; the owner's say, who may change it when accepting."""


@dataclass(frozen=True)
class BuildPermitView:
    """An agreed commission: the builder may build `kind` on the owner's parcel."""

    parcel: str
    centre: Position
    kind: PermitKind
    owner_id: str
    builder_id: str
    fee: int
    """Coins set aside by the owner, paid to the builder when the facility is finished."""
    expires_in: float
    """Seconds left to start the job; after that the permit lapses and the fee goes back."""
    orientation: Orientation = "north_south"
    """Which way the owner has said a mall runs."""


@dataclass(frozen=True)
class SponsorView:
    name: str
    price_per_view: float
    """Coins it pays for one person who passed an ad slot."""
    budget_per_second: float
    """Coins its budget fills with each second."""
    budget_now: int
    """What it could spend at once; the budget stops filling at `BUDGET_CAP_SECONDS` of its
    rate."""


@dataclass(frozen=True)
class LandView:
    parcel_size: float
    grid: int
    """Parcels along each side; ids are `L<col>-<row>` from the north-west corner."""
    slot_capacity: int
    """Pieces of content an ad slot shows at once."""
    ad_content_lasts: float
    """Seconds a piece of ad content is shown for once put in an ad slot. Games in a game hall
    never go stale, but draw fewer visitors the longer they have been there, see
    `MyParcelView.content_appeal`."""
    pay_period: float
    """Seconds between paydays, which fall at multiples of it."""
    facilities_built: int
    """Facilities of any kind built in the whole town, each venue counting once."""
    visitors_drawn: int
    """Visitors the town draws now; it grows with `facilities_built`, see the system prompt."""
    sponsors: tuple[SponsorView, ...]
    """Who pays for views of ad slots; at a payday the sponsors who pay most buy the views of
    all slots first, and when their budgets run short every slot is paid for the same share of
    its views."""
    here: ParcelView | None
    """The parcel the resident stands in; None while walking."""
    mine: tuple[MyParcelView, ...]
    best_free: tuple[ParcelView, ...]
    """Free parcels with the most people passing now, best first."""
    stakes: tuple[StakeView, ...] = ()
    """Parcels of other residents of which the resident holds shares, and so is paid."""


@dataclass(frozen=True)
class NearbyResident:
    id: str
    name: str
    position: Position
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
class ShareOfferView:
    event_id: int
    from_id: str
    to_id: str
    parcel: str
    shares: int
    price: int
    """Coins for all the shares; 0 when they are given away."""
    cut_last_payday: int
    """What the shares were paid at the last payday that paid the slot, whoever holds them."""


@dataclass(frozen=True)
class JointPurchaseView:
    event_id: int
    from_id: str
    to_id: str
    parcel: str
    centre: Position
    people_near: float
    """People passing the parcel's centre now."""
    price: int
    """What the parcel costs from the town."""
    from_pays: int
    """Coins the proposer pays when the recipient accepts; nothing is set aside before."""
    to_pays: int
    """Coins the recipient pays when they accept."""
    to_shares: int
    """Shares of the parcel's 100 the recipient gets; the proposer keeps the rest. Both may
    build on it."""
    parcel_taken: bool
    """Someone has bought the parcel meanwhile: accepting would fail, so the proposer should
    withdraw it, or the recipient reject it."""
    expires_in: float
    """Game seconds until the offer lapses if nobody answers."""


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
    exchange: ExchangeView
    compute: ComputeView
    land: LandView
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
    notices: tuple[NoticeView, ...] = ()
    """What the server announced to everyone since the resident last looked."""
    share_offers_to_me: tuple[ShareOfferView, ...] = ()
    my_share_offers: tuple[ShareOfferView, ...] = ()
    joint_purchases_to_me: tuple[JointPurchaseView, ...] = ()
    my_joint_purchases: tuple[JointPurchaseView, ...] = ()
    transport: TransportView | None = None
    link_proposals_to_me: tuple[LinkProposalView, ...] = ()
    my_link_proposals: tuple[LinkProposalView, ...] = ()
    build_commissions_to_me: tuple[BuildCommissionView, ...] = ()
    my_build_commissions: tuple[BuildCommissionView, ...] = ()
    build_permits: tuple[BuildPermitView, ...] = ()
    """Permits where the resident is the builder, who may build now, or the owner, whose
    parcel is to be built on."""
    recent_payments: RecentPayments = RecentPayments(count=0, coins=0)
    """Payments between residents over the past five minutes, see `RecentPayments`."""
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
    walk = me.walking
    standings = ranks(world)
    return Observation(
        time=perception.time,
        remaining_seconds=remaining,
        last_thinking_seconds=thinking[-1] if thinking else None,
        avg_thinking_seconds=sum(thinking) / len(thinking) if thinking else None,
        me=Me(
            id=me.id,
            name=me.name,
            position=here,
            coins=me.coins,
            coins_in_offers=reserved_coins(world, resident_id)
            + commission_coins(world, resident_id),
            coins_in_orders=order_coins(world.orders, resident_id),
            coins_in_contracts=contract_coins(world, resident_id),
            items=me.inventory.counts(),
            walking_to=walk.target if walk is not None else None,
            arrives_in=max(walk.arrives_at - world.time, 0.0) if walk is not None else None,
        ),
        exchange=_exchange_view(world, resident_id),
        recent_payments=_recent_payments(world),
        compute=_compute_view(world, resident_id),
        land=_land_view(world, resident_id, here, standing=walk is None),
        nearby_residents=tuple(
            NearbyResident(
                id=sighting.resident_id,
                name=sighting.name,
                position=sighting.position,
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
            )
            for resident in world.residents
        ),
        offers_to_me=_offers(pending_for(world, resident_id)),
        my_offers=_offers(pending_from(world, resident_id)),
        share_offers_to_me=_share_offers(world, pending_for(world, resident_id)),
        my_share_offers=_share_offers(world, pending_from(world, resident_id)),
        joint_purchases_to_me=_joint_purchases(world, pending_for(world, resident_id)),
        my_joint_purchases=_joint_purchases(world, pending_from(world, resident_id)),
        transport=_transport_view(world, resident_id, here, standing=walk is None),
        link_proposals_to_me=_link_proposals(world, pending_for(world, resident_id)),
        my_link_proposals=_link_proposals(world, pending_from(world, resident_id)),
        build_commissions_to_me=_build_commissions(world, pending_for(world, resident_id)),
        my_build_commissions=_build_commissions(world, pending_from(world, resident_id)),
        build_permits=_build_permits(world, resident_id),
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
            Happening(entry.time, entry.resident_id, entry.kind, entry.position)
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
    built = {parcel.id: parcel.facility.kind for parcel in world.parcels if parcel.facility}
    building = {
        job.parcel_id: max(job.ends_at - world.time, 0.0)
        for job in world.compute.jobs
        if resident_id in (job.owner_id, job.fee_from) and job.parcel_id is not None
    }
    facilities = {parcel.id: parcel for parcel in world.parcels if parcel.facility}
    traffic = Traffic(world)
    owned = {parcel.id: parcel for parcel in world.parcels}
    taken = claimed(world)

    def view(square: Plot) -> ParcelView:
        return ParcelView(
            square.id,
            square.centre,
            square.price,
            square.owner_id,
            traffic.near(square.centre),
            _distance(here, square.centre),
            walk_seconds(here, square.centre),
            built.get(square.id),
            building.get(square.id),
            len(_goes_stale_in(world, facilities.get(square.id))),
        )

    squares = plots(world)
    # Where one stands is shown if it is owned or for sale, not in the lobby.
    standing_on = plot_at(world, here)
    square_here = (
        standing_on
        if standing_on.owner_id is not None or for_sale_at(world, here) is not None
        else None
    )
    # Shares in a parcel let a resident build on it and put content in its facility, so those
    # parcels are listed too.
    shared = {parcel.id for parcel in world.parcels if parcel.held_by(resident_id) > 0}
    mine = [square for square in squares if square.owner_id == resident_id or square.id in shared]
    cells = world.map.block_cells
    best = sorted(
        # One suggestion per block, its middle parcel, since a block is bought whole.
        (
            square
            for square in squares
            if square.owner_id is None
            and not square.lobby
            and square.col % cells == cells // 2
            and square.row % cells == cells // 2
        ),
        key=lambda square: (
            -traffic.near(square.centre),
            square.price,
            _distance(here, square.centre),
        ),
    )[:LAND_SHOWN]

    def mine_view(square: Plot) -> MyParcelView:
        parcel = owned[square.id]
        kind = built.get(square.id)
        built_on = facilities.get(square.id)
        runs_as = built_on.facility.orientation if built_on and built_on.facility else None
        centre = facility_centre(world.map, square.col, square.row, kind, runs_as or "north_south")
        holder = taken.get((square.col, square.row))
        return MyParcelView(
            square.id,
            centre,
            traffic.near(centre),
            _distance(here, centre),
            walk_seconds(here, centre),
            kind,
            building.get(square.id),
            _goes_stale_in(world, facilities.get(square.id)),
            _income(world, facilities.get(square.id)),
            dict(parcel.shares),
            _cut(world, facilities.get(square.id), resident_id),
            round(world.crowd.viewed(square.id), 1),
            round(world.admissions.admitted_to(square.id), 1),
            holder if holder != square.id else None,
            venue_room(world, parcel, taken, resident_id=resident_id)
            and promised_builder(world, square.id, "game_hall") is None,
            promised_builder(world, square.id, None),
            tuple(
                runs
                for runs in ORIENTATIONS
                if venue_room(world, parcel, taken, "mall", runs, resident_id)
                and promised_builder(world, square.id, "mall", runs) is None
            ),
            parcel.owner_id == resident_id and garden_room(world, parcel, taken),
            block_has_garden(world, square.id),
            _appeal(world, facilities.get(square.id)),
            portal_room(world, parcel, taken),
        )

    return LandView(
        parcel_size=PARCEL_SIZE,
        grid=grid_size(world),
        slot_capacity=AD_SLOT_CAPACITY,
        ad_content_lasts=CONTENT_LIFETIME,
        pay_period=PAY_PERIOD,
        facilities_built=facilities_built(world),
        visitors_drawn=visitors_drawn(world),
        sponsors=_sponsor_views(world),
        here=view(square_here) if standing and square_here is not None else None,
        mine=tuple(mine_view(square) for square in mine),
        best_free=tuple(view(square) for square in best),
        stakes=tuple(
            StakeView(
                parcel.id,
                parcel.owner_id,
                parcel.held_by(resident_id),
                dict(parcel.shares),
                parcel.facility.kind if parcel.facility else None,
                len(_goes_stale_in(world, parcel)),
                _income(world, parcel),
                _cut(world, parcel, resident_id),
            )
            for parcel in world.parcels
            if parcel.owner_id != resident_id and parcel.held_by(resident_id)
        ),
    )


def _income(world: World, parcel: Parcel | None) -> int:
    """Coins the facility on `parcel` was paid at the last payday, from sponsors, from fees at
    the door and from visitors' tolls, 0 without."""
    if parcel is None:
        return 0
    return world.sponsors.last_paid_to(parcel.id) + world.admissions.last_paid_to(parcel.id)


def _cut(world: World, parcel: Parcel | None, resident_id: str) -> int:
    """The part of that going to `resident_id`."""
    if parcel is None:
        return 0
    return dict(split_income(parcel, _income(world, parcel))).get(resident_id, 0)


def _sponsor_views(world: World) -> tuple[SponsorView, ...]:
    book = world.sponsors
    elapsed = world.time - book.topped_up_at
    return tuple(
        SponsorView(
            sponsor.name,
            sponsor.price,
            sponsor.rate,
            round(min(sponsor.funds + sponsor.rate * elapsed, sponsor.rate * BUDGET_CAP_SECONDS)),
        )
        for sponsor in book.sponsors
    )


def _appeal(world: World, parcel: Parcel | None) -> float:
    """What the content shown on `parcel` is worth to visitors now, in pieces, to a tenth."""
    if parcel is None or parcel.facility is None or parcel.facility.kind not in ITEM_FOR:
        return 0.0
    return round(appeal(parcel.facility, world.time), 1)


def _goes_stale_in(world: World, parcel: Parcel | None) -> tuple[float, ...]:
    if parcel is None or parcel.facility is None:
        return ()
    return tuple(
        stale_at - world.time for stale_at in parcel.facility.contents if stale_at > world.time
    )


def _compute_view(world: World, resident_id: str) -> ComputeView:
    book = world.compute
    now = world.time

    def job_view(job: Job) -> JobView:
        return JobView(
            job.id,
            job.kind,
            job.parcel_id,
            job.source,
            job.node_id,
            max(job.ends_at - now, 0.0),
        )

    mine = sorted((job for job in book.jobs if job.owner_id == resident_id), key=_soonest)
    on_node = {job.node_id: job for job in mine if job.node_id is not None}
    nodes = tuple(
        NodeView(
            node.id,
            node.kind,
            max(node.expires_at - now, 0.0),
            on_node[node.id].id if node.id in on_node else None,
            # A node that runs out first is not free for longer than it exists.
            min(max(on_node[node.id].ends_at - now, 0.0), max(node.expires_at - now, 0.0))
            if node.id in on_node
            else 0.0,
        )
        for node in book.nodes
        if node.owner_id == resident_id
    )
    return ComputeView(
        own_job=next((job_view(job) for job in mine if job.source == "self"), None),
        my_nodes=nodes,
        my_jobs=tuple(job_view(job) for job in mine),
        ad_slot=(
            AdOption("self", job_seconds(AD_WORKLOAD, "self"), 0),
            AdOption(
                "on_demand", job_seconds(AD_WORKLOAD, "on_demand"), on_demand_cost(AD_WORKLOAD)
            ),
            AdOption("subscription", job_seconds(AD_WORKLOAD, "subscription"), 0),
        ),
        portal=(
            AdOption("self", job_seconds(PORTAL_WORKLOAD, "self"), 0),
            AdOption(
                "on_demand",
                job_seconds(PORTAL_WORKLOAD, "on_demand"),
                on_demand_cost(PORTAL_WORKLOAD),
            ),
            AdOption("subscription", job_seconds(PORTAL_WORKLOAD, "subscription"), 0),
        ),
        venue=_options(VENUE_WORKLOAD),
        mall=_options(MALL_WORKLOAD),
        garden=_options(GARDEN_WORKLOAD),
        content=_options(making_workload(world, resident_id, "make_content")),
        game=_options(making_workload(world, resident_id, "make_game")),
        node_coins_per_second=SUBSCRIPTION_PRICE,
        node_seconds=NODE_SECONDS,
        max_nodes=MAX_NODES,
        town_nodes=tuple(_town_nodes(world, kind) for kind in NODE_KINDS),
        practice=tuple(
            PracticeView(
                content,
                pieces_made(world, resident_id, job),
                round(100 * practice_factor(pieces_made(world, resident_id, job))),
            )
            for content, job in MAKE_JOB.items()
        ),
    )


def _town_nodes(world: World, kind: NodeKind) -> TownNodesView:
    now = world.time
    book = world.compute
    held: dict[str, int] = {}
    for node in book.nodes:
        if node.kind == kind and node.expires_at > now:
            held[node.owner_id] = held.get(node.owner_id, 0) + 1
    hired = sum(
        job.source == "on_demand" and NODE_FOR_JOB[job.kind] == kind and job.ends_at > now
        for job in book.jobs
    )
    taken = nodes_in_use(world, kind)
    return TownNodesView(
        kind=kind,
        total=node_supply(world),
        free=nodes_free(world, kind),
        held_by=held,
        hired=hired,
        next_free_in=taken[0] - now if taken else None,
    )


def _options(workload: float) -> tuple[AdOption, ...]:
    return (
        AdOption("self", job_seconds(workload, "self"), 0),
        AdOption("on_demand", job_seconds(workload, "on_demand"), on_demand_cost(workload)),
        AdOption("subscription", job_seconds(workload, "subscription"), 0),
    )


def _soonest(job: Job) -> tuple[float, int]:
    return job.ends_at, job.id


def _recent_payments(world: World) -> RecentPayments:
    paid = [
        transfer
        for transfer in world.ledger.transfers
        if SYSTEM not in (transfer.payer, transfer.payee)
        and world.time - TREND_SECONDS <= transfer.time <= world.time
    ]
    return RecentPayments(count=len(paid), coins=sum(transfer.amount for transfer in paid))


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


def _share_offers(world: World, events: tuple[Event, ...]) -> tuple[ShareOfferView, ...]:
    """The share offers among `events`."""
    parcels = {parcel.id: parcel for parcel in world.parcels}
    views = []
    for event in events:
        offer = event.payload
        if not isinstance(offer, ShareOffer):
            continue
        parcel = parcels[offer.parcel_id]
        views.append(
            ShareOfferView(
                event_id=event.id,
                from_id=event.sender_id,
                to_id=event.recipient_id,
                parcel=offer.parcel_id,
                shares=offer.shares,
                price=offer.price,
                cut_last_payday=_income(world, parcel) * offer.shares // PARCEL_SHARES,
            )
        )
    return tuple(views)


PORTALS_SHOWN = 12
"""Portals of other residents an observation lists."""


def _transport_view(
    world: World, resident_id: str, here: Position, *, standing: bool
) -> TransportView:
    def portal_view(parcel: Parcel) -> PortalView:
        centre = plot(world, parcel.col, parcel.row).centre
        lines = []
        for line in links_of(world, parcel.id):
            far = next(p for p in world.parcels if p.id == line.other(parcel.id))
            lines.append(
                LineView(far.id, far.owner_id, plot(world, far.col, far.row).centre, line.toll)
            )
        return PortalView(
            parcel.id, parcel.owner_id, centre, people_near(world, centre), tuple(lines)
        )

    portals = [p for p in world.parcels if p.facility is not None and p.facility.kind == "portal"]
    town = town_centre(world.map.size)

    def from_the_middle(parcel: Parcel) -> tuple[float, str]:
        centre = plot(world, parcel.col, parcel.row).centre
        return math.dist((centre.x, centre.y), (town.x, town.y)), parcel.id

    gone = {r.id for r in world.residents if r.left}
    # A portal whose owner left stays, but can no longer be joined to anything new.
    others = sorted(
        (p for p in portals if p.owner_id != resident_id and p.owner_id not in gone),
        key=from_the_middle,
    )
    standing_in = plot_at(world, here).id
    return TransportView(
        max_portals=MAX_PORTALS,
        max_lines_per_portal=MAX_LINKS_PER_PORTAL,
        min_line_distance=MIN_LINK_DISTANCE,
        max_toll=MAX_TOLL,
        here=next((portal_view(p) for p in portals if p.id == standing_in), None)
        if standing
        else None,
        mine=tuple(portal_view(p) for p in portals if p.owner_id == resident_id),
        others=tuple(portal_view(p) for p in others[:PORTALS_SHOWN]),
    )


def _link_proposals(world: World, events: tuple[Event, ...]) -> tuple[LinkProposalView, ...]:
    """The line proposals among `events`."""
    views = []
    for event in events:
        proposal = event.payload
        if not isinstance(proposal, LinkProposal):
            continue
        views.append(
            LinkProposalView(
                event_id=event.id,
                from_id=event.sender_id,
                to_id=event.recipient_id,
                from_parcel=proposal.parcel_id,
                to_parcel=proposal.other_parcel_id,
                from_centre=_centre_of(world, proposal.parcel_id),
                to_centre=_centre_of(world, proposal.other_parcel_id),
                toll=proposal.toll,
            )
        )
    return tuple(views)


def _build_commissions(world: World, events: tuple[Event, ...]) -> tuple[BuildCommissionView, ...]:
    """The build commissions among `events`."""
    return tuple(
        BuildCommissionView(
            event_id=event.id,
            from_id=event.sender_id,
            to_id=event.recipient_id,
            parcel=event.payload.parcel_id,
            centre=_centre_of(world, event.payload.parcel_id),
            kind=event.payload.kind,
            owner_id=event.payload.owner_id,
            builder_id=event.payload.builder_id,
            fee=event.payload.fee,
            orientation=event.payload.orientation,
        )
        for event in events
        if isinstance(event.payload, BuildCommission)
    )


def _build_permits(world: World, resident_id: str) -> tuple[BuildPermitView, ...]:
    """The permits the resident holds as builder or has given as owner."""
    return tuple(
        BuildPermitView(
            parcel=permit.parcel_id,
            centre=_centre_of(world, permit.parcel_id),
            kind=permit.kind,
            owner_id=permit.owner_id,
            builder_id=permit.builder_id,
            fee=permit.fee,
            orientation=permit.orientation,
            expires_in=max(permit.expires_at - world.time, 0.0),
        )
        for permit in world.compute.permits
        if resident_id in (permit.owner_id, permit.builder_id)
    )


def _centre_of(world: World, parcel_id: str) -> Position:
    parcel = next(p for p in world.parcels if p.id == parcel_id)
    return plot(world, parcel.col, parcel.row).centre


def _joint_purchases(world: World, events: tuple[Event, ...]) -> tuple[JointPurchaseView, ...]:
    """The joint purchases among `events`."""
    views = []
    for event in events:
        proposal = event.payload
        if not isinstance(proposal, JointPurchase):
            continue
        square = plot(world, proposal.col, proposal.row)
        views.append(
            JointPurchaseView(
                event_id=event.id,
                from_id=event.sender_id,
                to_id=event.recipient_id,
                parcel=square.id,
                centre=square.centre,
                people_near=people_near(world, square.centre),
                price=square.price,
                from_pays=proposal.proposer_pays(world),
                to_pays=proposal.partner_pays,
                to_shares=proposal.partner_shares,
                parcel_taken=square.owner_id is not None,
                expires_in=max(proposal.expires_at - world.time, 0.0),
            )
        )
    return tuple(views)


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
