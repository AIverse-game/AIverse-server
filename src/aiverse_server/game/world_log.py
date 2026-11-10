"""World log: everything residents do, for the pages' world log panel.

Unlike the activity log, which residents see only near where they stand (`perception.py`), and
speech, of which a page sees only its viewer's whispers, the world log is the whole town's
record for the people watching: every primitive a resident carries out, whispers and offers to
anyone included, and every build or piece of content that finishes. Entries are structured, see
`core.world.WorldLogEntry`; pages put them into words.

`perform_logged` carries a primitive out as `actions.perform` does and logs it; the server and
the agents' tools use it rather than `perform`. The clock logs finished jobs through
`record_finished_jobs`, see `clock.settle_due`. Refused primitives change nothing, so they are
never logged. The log keeps the latest `WORLD_LOG_LIMIT` entries.

Like the rest of the world, every function returns a new world and leaves its argument untouched.
"""

from dataclasses import replace
from typing import Literal

from ..core.compute import Job
from ..core.events import Event, EventPayload
from ..core.land import parcel_id
from ..core.world import (
    WORLD_LOG_LIMIT,
    DealKind,
    Position,
    World,
    WorldLog,
    WorldLogEntry,
    WorldLogVerb,
)
from ..economy.commissions import BuildCommission
from ..economy.compute import BUILT_BY
from ..economy.contracts import ContractProposal
from ..economy.joint_purchase import JointPurchase
from ..economy.land import plot_at
from ..economy.portals import LinkProposal
from ..economy.shares import ShareOffer
from ..economy.trade import PurchaseOffer, SaleOffer
from ..residents.information_offer import InformationOffer
from .actions import (
    Action,
    BuildAd,
    BuildPortal,
    BuildVenue,
    BuyNode,
    BuyParcel,
    CancelContract,
    CancelOrder,
    Deliver,
    LeaveGame,
    LinkPortals,
    MakeContent,
    MoveTo,
    OfferBuildCommission,
    OfferInformation,
    OfferJointPurchase,
    OfferShares,
    OfferToBuy,
    OfferToSell,
    PlaceContent,
    PlaceOrder,
    ProposeContract,
    RespondToEvent,
    Say,
    Teleport,
    WithdrawEvent,
    perform,
)


def perform_logged(world: World, action: Action) -> World:
    """Carry out `action` as `actions.perform` does, raising `ActionRejected` the same way, and
    add what was done to the world log."""
    return record_action(world, action, perform(world, action))


def record_action(before: World, action: Action, after: World) -> World:
    """Log, at `after.time`, what `action` did in taking `before` to `after`."""
    entry = _action_entry(before, action, after)
    return after if entry is None else _append(after, [entry])


def record_finished_jobs(before: World, after: World) -> World:
    """Log, at `after.time`, every job running in `before` that is gone from `after`: a facility
    built or a piece of content made. Only `finish_jobs` may lie between the two worlds."""
    running = {job.id for job in after.compute.jobs}
    bare = {parcel.id for parcel in before.parcels if parcel.facility is None}
    fresh = {
        parcel.id: parcel.facility.kind
        for parcel in after.parcels
        if parcel.facility is not None and parcel.id in bare
    }
    entries: list[WorldLogEntry] = []
    for job in sorted(before.compute.jobs, key=lambda job: (job.ends_at, job.id)):
        if job.id in running:
            continue
        if job.parcel_id is not None:
            # A job dropped because another on its parcel finished first built nothing.
            if fresh.get(job.parcel_id) != BUILT_BY[job.kind]:
                continue
            del fresh[job.parcel_id]
        entries.append(_finished(job))
    return _append(after, entries) if entries else after


def _finished(job: Job) -> WorldLogEntry:
    if job.parcel_id is None:
        item = "game" if job.kind == "make_game" else "ad"
        return _entry("made", job.owner_id, item=item)
    return _entry(
        "built",
        job.owner_id,
        other_id=job.fee_from,
        parcel_id=job.parcel_id,
        facility=BUILT_BY[job.kind],
    )


def _action_entry(before: World, action: Action, after: World) -> WorldLogEntry | None:
    me = action.resident_id
    match action:
        case Say():
            line = after.speech.entries[-1]
            listeners = None if line.to is not None else len(line.listeners)
            return _entry("said", me, other_id=line.to, text=line.text, quantity=listeners)
        case MoveTo():
            return _entry("walked", me, position=action.target)
        case Teleport():
            start = plot_at(before, _resident_position(before, me)).id
            # The line's toll, not what the traveller is out of pocket: holding shares of either
            # end hands them part of it back.
            toll = next(
                line.toll
                for line in before.links
                if line.joins(start) and line.other(start) == action.to_parcel_id
            )
            return _entry(
                "teleported",
                me,
                parcel_id=start,
                other_parcel_id=action.to_parcel_id,
                coins=toll,
            )
        case LeaveGame():
            return _entry("left_game", me)
        case BuyParcel():
            owned = {parcel.id for parcel in before.parcels}
            bought = [parcel for parcel in after.parcels if parcel.id not in owned]
            return _entry(
                "bought_parcel",
                me,
                parcel_id=plot_at(before, _resident_position(before, me)).id,
                quantity=len(bought),
                coins=_spent(before, after, me),
            )
        case BuyNode():
            return _entry(
                "bought_node",
                me,
                item=action.kind,
                quantity=action.seconds,
                coins=_spent(before, after, me),
            )
        case BuildAd() | BuildPortal() | BuildVenue():
            facility = (
                "ad"
                if isinstance(action, BuildAd)
                else "portal"
                if isinstance(action, BuildPortal)
                else action.kind
            )
            return _entry(
                "started_building",
                me,
                other_id=_other_owner(after, action.parcel_id, me),
                parcel_id=action.parcel_id,
                facility=facility,
            )
        case MakeContent():
            return _entry("started_making", me, item=action.content)
        case PlaceContent():
            parcel = next(p for p in after.parcels if p.id == action.parcel_id)
            return _entry(
                "placed_content",
                me,
                other_id=_other_owner(after, action.parcel_id, me),
                parcel_id=action.parcel_id,
                facility=None if parcel.facility is None else parcel.facility.kind,
            )
        case LinkPortals():
            sent = _sent_event(before, after)
            if sent is not None:
                return _deal_entry("offered", me, sent.recipient_id, sent)
            return _entry(
                "linked_portals",
                me,
                parcel_id=action.parcel_id,
                other_parcel_id=action.other_parcel_id,
                coins=action.toll,
            )
        case (
            OfferToBuy()
            | OfferToSell()
            | OfferInformation()
            | OfferShares()
            | ProposeContract()
            | OfferJointPurchase()
            | OfferBuildCommission()
        ):
            sent = _sent_event(before, after)
            return None if sent is None else _deal_entry("offered", me, sent.recipient_id, sent)
        case RespondToEvent() | WithdrawEvent():
            event = next(e for e in before.events.pending if e.id == action.event_id)
            if isinstance(action, WithdrawEvent):
                return _deal_entry("withdrew", me, event.recipient_id, event)
            answer: WorldLogVerb = "accepted" if action.accept else "declined"
            return _deal_entry(answer, me, event.sender_id, event)
        case Deliver() | CancelContract():
            contract = next(c for c in before.contracts.active if c.id == action.contract_id)
            if isinstance(action, CancelContract):
                return _entry("cancelled_contract", me, other_id=contract.buyer_id)
            return _entry(
                "delivered",
                me,
                other_id=contract.buyer_id,
                item=contract.kind,
                quantity=contract.quantity,
                coins=contract.price,
            )
        case PlaceOrder():
            return _entry(
                "placed_order",
                me,
                side=action.side,
                item=action.item,
                quantity=action.quantity,
                coins=action.price,
            )
        case CancelOrder():
            order = next(o for o in before.orders.orders if o.id == action.order_id)
            return _entry("cancelled_order", me, side=order.side, item=order.item)
    return None


def _deal_entry(
    verb: WorldLogVerb, actor_id: str, other_id: str, event: Event
) -> WorldLogEntry | None:
    """An entry about the request `event`, with what it asked for; None for a kind of request
    the log does not know."""
    payload: EventPayload = event.payload
    match payload:
        case PurchaseOffer() | SaleOffer():
            name: DealKind = "purchase" if isinstance(payload, PurchaseOffer) else "sale"
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal=name,
                item=payload.kind,
                quantity=payload.quantity,
                coins=payload.price,
            )
        case InformationOffer():
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal="information",
                text=payload.summary,
                coins=payload.price,
            )
        case ShareOffer():
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal="shares",
                parcel_id=payload.parcel_id,
                quantity=payload.shares,
                coins=payload.price,
            )
        case ContractProposal():
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal="contract",
                side="sell" if payload.sender_supplies else "buy",
                item=payload.kind,
                quantity=payload.quantity,
                coins=payload.price,
                deliveries=payload.deliveries,
            )
        case JointPurchase():
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal="joint_purchase",
                parcel_id=parcel_id(payload.col, payload.row),
                quantity=payload.partner_shares,
                coins=payload.partner_pays,
            )
        case BuildCommission():
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal="commission",
                side="buy" if event.sender_id == payload.owner_id else "sell",
                parcel_id=payload.parcel_id,
                facility=payload.kind,
                coins=payload.fee,
            )
        case LinkProposal():
            return _entry(
                verb,
                actor_id,
                other_id=other_id,
                deal="link",
                parcel_id=payload.parcel_id,
                other_parcel_id=payload.other_parcel_id,
                coins=payload.toll,
            )
    return None


def _entry(
    verb: WorldLogVerb,
    actor_id: str,
    *,
    other_id: str | None = None,
    text: str | None = None,
    parcel_id: str | None = None,
    other_parcel_id: str | None = None,
    facility: str | None = None,
    item: str | None = None,
    quantity: int | None = None,
    coins: int | None = None,
    deliveries: int | None = None,
    position: Position | None = None,
    deal: DealKind | None = None,
    side: Literal["buy", "sell"] | None = None,
) -> WorldLogEntry:
    # Numbered and timed by `_append`; these are placeholders.
    return WorldLogEntry(
        seq=0,
        time=0.0,
        verb=verb,
        actor_id=actor_id,
        other_id=other_id,
        text=text,
        parcel_id=parcel_id,
        other_parcel_id=other_parcel_id,
        facility=facility,
        item=item,
        quantity=quantity,
        coins=coins,
        deliveries=deliveries,
        position=position,
        deal=deal,
        side=side,
    )


def _append(world: World, found: list[WorldLogEntry]) -> World:
    log = world.world_log
    numbered = tuple(
        replace(entry, seq=log.next_seq + index, time=world.time)
        for index, entry in enumerate(found)
    )
    kept = (*log.entries, *numbered)[-WORLD_LOG_LIMIT:]
    return replace(world, world_log=WorldLog(kept, log.next_seq + len(numbered)))


def _sent_event(before: World, after: World) -> Event | None:
    """The event the action put on the queue, if it sent one."""
    return next((e for e in after.events.pending if e.id >= before.events.next_id), None)


def _resident_position(world: World, resident_id: str) -> Position:
    return next(r.position for r in world.residents if r.id == resident_id)


def _spent(before: World, after: World, resident_id: str) -> int:
    def coins(world: World) -> int:
        return next(r.coins for r in world.residents if r.id == resident_id)

    return coins(before) - coins(after)


def _other_owner(world: World, parcel: str, resident_id: str) -> str | None:
    """The parcel's owner when it is not `resident_id`, as when building on a permit."""
    owner = next((p.owner_id for p in world.parcels if p.id == parcel), None)
    return None if owner == resident_id else owner
