"""What an agent is shown at the start of each round: its perception, put together with what
the resident knows of themselves and what is public.

`observe` adds to the perception (`perception.py`) the resident's own coins, tokens, chips
and what they are busy with; the offers and contract proposals waiting for them and those
they sent, the contracts they are party to and the last few of theirs that ended; and every
resident's name, coins and place, which the page shows anyway and which an agent needs to
whisper or trade. Other residents' positions stay limited to those in sight. Distances and
walking costs are worked out from where the resident stands, so a model need not do the
arithmetic. `to_json` turns an observation into compact JSON for a model.
"""

import json
import math
from dataclasses import asdict, dataclass

from ..core.events import Event, pending_for, pending_from
from ..core.rigs import MiningRig, RigSlot
from ..core.world import Contract, ContractEnd, PoolSighting, Position, World
from ..economy.chips import CHIP_MODELS
from ..economy.contracts import ContractProposal, contract_coins, contracts_of
from ..economy.rigs import (
    ENERGY_PER_100,
    RENT_PER_100,
    RIG_CAPACITY,
    RIG_PRICE,
    RIG_SECONDS,
    RIG_YIELD,
    cycle_amount,
    cycle_cost,
    pool_draw_rate,
    rig_coins,
    slot_status,
)
from ..economy.tokens import TOKEN, walk_cost
from ..economy.trade import PurchaseOffer, SaleOffer, reserved_coins, reserved_items
from ..game.lifecycle import ranks, total_coins
from ..residents.information_offer import InformationOffer
from ..residents.perception import Perception
from ..residents.walking import walk_seconds

ENDED_CONTRACTS_SHOWN = 5
"""Ended contracts an observation lists, the latest."""


@dataclass(frozen=True)
class Me:
    id: str
    name: str
    position: Position
    coins: int
    """Spendable coins."""
    coins_in_offers: int
    """Coins set aside in purchase offers still waiting for an answer."""
    tokens: int
    """Tokens carried, which walking and speaking spend."""
    tokens_in_offers: int
    coins_in_contracts: int
    """Coins set aside as a buyer's payments or a supplier's deposits, in contracts in force
    and in proposals not yet answered."""
    chips: dict[str, int]
    """Chips carried, model to count."""
    walking_to: Position | None
    arrives_in: float | None
    """Seconds until the walk ends."""
    mining_pool: str | None
    mining_done_in: float | None
    """Seconds until the mining job pays out."""
    at_market: bool
    at_pool: str | None
    """The pool the resident stands close enough to mine, if any."""


@dataclass(frozen=True)
class ChipOnSale:
    model: str
    price: int
    tokens_per_job: int


@dataclass(frozen=True)
class MarketView:
    position: Position
    radius: float
    token_price: float
    """Coins one more token fetches right now; it falls as tokens are sold."""
    distance: float
    walk_cost: int
    """Tokens a walk from here to the market's centre costs."""
    walk_seconds: float
    chips: tuple[ChipOnSale, ...]


@dataclass(frozen=True)
class PoolView:
    id: str
    position: Position
    radius: float
    reserve: int
    """Tokens left when last seen."""
    seen_ago: float
    """Seconds since the reserve was seen; 0 when in sight."""
    in_sight: bool
    """Seen this round, so the reserve is current."""
    rig_draw_per_second: float | None
    """Tokens per second the running rigs on this pool take out of it; None unless in sight,
    since a pool out of sight is not watched. Manual mining is not counted."""
    seconds_to_empty: float | None
    """Seconds until the reserve is gone at that rate; None unless in sight and rigs are
    drawing on it."""
    distance: float
    walk_cost: int
    walk_seconds: float


@dataclass(frozen=True)
class NearbyResident:
    id: str
    name: str
    position: Position
    mining_pool: str | None
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
    pool_id: str | None


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
    known_pools: tuple[PoolView, ...]
    """Every pool discovered so far, nearest first."""
    discovered: tuple[str, ...]
    """Pools seen for the first time since the last round: from where the resident stands, or
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
    rig_balance: int
    rig_rules: dict[str, int | float]
    mining_rigs: tuple[dict[str, object], ...]
    notices: tuple[NoticeView, ...] = ()
    """What the server announced to everyone since the resident last looked."""


def observe(
    world: World, resident_id: str, perception: Perception, remaining: float
) -> Observation:
    """Put the resident's perception together with what they know of themselves and the public
    ranking; `remaining` is the game time left."""
    me = next(resident for resident in world.residents if resident.id == resident_id)
    here = perception.position
    chip_models = {chip.model for chip in CHIP_MODELS}
    walk = me.walking
    job = me.mining
    market = world.market
    standings = ranks(world)
    at_pool = next(
        (pool.id for pool in world.pools if pool.reaches(here) and walk is None),
        None,
    )
    in_sight = frozenset(sighting.pool_id for sighting in perception.pools)
    # A neighbour's or a happening's pool is named only if the resident has seen that pool.
    known_ids = {sighting.pool_id for sighting in perception.known_pools}
    pools = sorted(
        (
            _pool_view(world, sighting, here, sighting.pool_id in in_sight)
            for sighting in perception.known_pools
        ),
        key=lambda pool: pool.distance,
    )
    return Observation(
        rig_balance=rig_coins(world, resident_id),
        rig_rules={
            "price": RIG_PRICE,
            "capacity": RIG_CAPACITY,
            "cycle_seconds": RIG_SECONDS,
            "yield_multiplier": RIG_YIELD,
            "rent_per_100": RENT_PER_100,
            "energy_per_100": ENERGY_PER_100,
        },
        mining_rigs=_rig_views(world, resident_id, in_sight),
        time=perception.time,
        remaining_seconds=remaining,
        me=Me(
            id=me.id,
            name=me.name,
            position=here,
            coins=me.coins,
            coins_in_offers=reserved_coins(world, resident_id),
            tokens=me.inventory.count(TOKEN),
            tokens_in_offers=reserved_items(world, resident_id, TOKEN),
            coins_in_contracts=contract_coins(world, resident_id),
            chips={
                kind: count for kind, count in me.inventory.counts().items() if kind in chip_models
            },
            walking_to=walk.target if walk is not None else None,
            arrives_in=max(walk.arrives_at - world.time, 0.0) if walk is not None else None,
            mining_pool=job.pool_id if job is not None else None,
            mining_done_in=max(job.ends_at - world.time, 0.0) if job is not None else None,
            at_market=market is not None and walk is None and market.reaches(here),
            at_pool=at_pool,
        ),
        market=None
        if perception.market is None
        else MarketView(
            position=perception.market.position,
            radius=perception.market.radius,
            token_price=perception.market.price,
            distance=_distance(here, perception.market.position),
            walk_cost=walk_cost(here, perception.market.position),
            walk_seconds=walk_seconds(here, perception.market.position),
            chips=tuple(
                ChipOnSale(chip.model, chip.price, chip.tokens_per_job)
                for chip in perception.market.chips
            ),
        ),
        known_pools=tuple(pools),
        discovered=perception.discovered,
        nearby_residents=tuple(
            NearbyResident(
                id=sighting.resident_id,
                name=sighting.name,
                position=sighting.position,
                mining_pool=sighting.mining_pool_id
                if sighting.mining_pool_id in known_ids
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
                entry.pool_id if entry.pool_id in known_ids else None,
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
    rig: MiningRig, resident_id: str, in_sight: frozenset[str]
) -> tuple[RigSlot, ...] | None:
    """The slots of a machine the resident can know of: all of them when its pool is in sight
    or the resident owns it, else only their own; None when they know nothing of it. A machine
    on a pool they have not seen would give the pool's state away."""
    if rig.pool_id in in_sight or rig.owner_id == resident_id:
        return rig.slots
    mine = tuple(slot for slot in rig.slots if slot.resident_id == resident_id)
    return mine or None


def _rig_views(
    world: World, resident_id: str, in_sight: frozenset[str]
) -> tuple[dict[str, object], ...]:
    """The machines the resident can know of, see `known_slots`."""
    views: list[dict[str, object]] = []
    for rig in world.rigs:
        slots = known_slots(rig, resident_id, in_sight)
        if slots is None:
            continue
        views.append(
            {
                "id": rig.id,
                "owner_id": rig.owner_id,
                "pool_id": rig.pool_id,
                "position": asdict(next(p.position for p in world.pools if p.id == rig.pool_id)),
                "slots": [
                    {
                        "id": s.id,
                        "resident_id": s.resident_id,
                        "chip": s.chip,
                        "balance": s.balance,
                        "mined": s.mined,
                        "rent_paid": s.rent_paid,
                        "status": slot_status(world, rig, s),
                        "next_in": max(s.next_at - world.time, 0.0),
                        "tokens_per_cycle": cycle_amount(world, rig, s),
                        "rent_and_energy": sum(cycle_cost(rig, s, cycle_amount(world, rig, s))),
                    }
                    for s in slots
                ],
            }
        )
    return tuple(views)


def _pool_view(world: World, sighting: PoolSighting, here: Position, in_sight: bool) -> PoolView:
    """A known pool; how fast rigs drain it is told only while the resident can see it."""
    draw = pool_draw_rate(world, sighting.pool_id) if in_sight else None
    return PoolView(
        id=sighting.pool_id,
        position=sighting.position,
        radius=sighting.radius,
        reserve=sighting.reserve,
        seen_ago=0.0 if in_sight else max(world.time - sighting.seen_at, 0.0),
        in_sight=in_sight,
        rig_draw_per_second=draw,
        seconds_to_empty=sighting.reserve / draw if draw else None,
        distance=_distance(here, sighting.position),
        walk_cost=walk_cost(here, sighting.position),
        walk_seconds=walk_seconds(here, sighting.position),
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
