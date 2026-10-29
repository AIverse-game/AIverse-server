"""What an agent is shown at the start of each round: its perception, put together with what
the resident knows of themselves and what is public.

`observe` adds to the perception (`perception.py`) the resident's own coins, tokens, chips
and what they are busy with; the offers waiting for them and those they sent; and every
resident's name, coins and place, which the page shows anyway and which an agent needs to
whisper or trade. Other residents' positions stay limited to those in sight. Distances and
walking costs are worked out from where the resident stands, so a model need not do the
arithmetic. `to_json` turns an observation into compact JSON for a model.
"""

import json
import math
from dataclasses import asdict, dataclass

from ..chips import CHIP_MODELS
from ..events import Event, pending_for, pending_from
from ..lifecycle import ranks, total_coins
from ..perception import Perception
from ..tokens import TOKEN, walk_cost
from ..trade import PurchaseOffer, SaleOffer, reserved_coins, reserved_items
from ..walking import walk_seconds
from ..world import Position, World


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
    in_sight: bool
    """Seen this round, so the reserve is current."""
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
class Line:
    time: float
    speaker_id: str
    text: str
    to: str | None
    """The one resident spoken to; None when said aloud."""


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
    """Pools seen for the first time this round."""
    nearby_residents: tuple[NearbyResident, ...]
    residents: tuple[Standing, ...]
    offers_to_me: tuple[OfferView, ...]
    my_offers: tuple[OfferView, ...]
    speech: tuple[Line, ...]
    """What was said in the resident's hearing since they last looked, their own words too."""
    activity: tuple[Happening, ...]
    """What others did nearby since the resident last looked."""
    long_term_notes: tuple[NoteView, ...]
    short_term_notes: tuple[NoteView, ...]


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
    in_sight = {sighting.pool_id for sighting in perception.pools}
    pools = sorted(
        (
            PoolView(
                id=sighting.pool_id,
                position=sighting.position,
                radius=sighting.radius,
                reserve=sighting.reserve,
                in_sight=sighting.pool_id in in_sight,
                distance=_distance(here, sighting.position),
                walk_cost=walk_cost(here, sighting.position),
                walk_seconds=walk_seconds(here, sighting.position),
            )
            for sighting in perception.known_pools
        ),
        key=lambda pool: pool.distance,
    )
    return Observation(
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
                mining_pool=sighting.mining_pool_id,
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
        speech=tuple(
            Line(line.time, line.speaker_id, line.text, line.to) for line in perception.speech
        ),
        activity=tuple(
            Happening(entry.time, entry.resident_id, entry.kind, entry.position, entry.pool_id)
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
    """The trade offers among `events`; trades are the only kind of event so far."""
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
