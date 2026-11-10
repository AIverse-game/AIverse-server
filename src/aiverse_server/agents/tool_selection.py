"""Choose the function schemas a resident needs for the current decision round.

The game still validates and executes every action through ``ResidentTools``. Discovery
only changes which schemas are advertised to the model; it does not grant capabilities.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.land import ORIENTATIONS
from ..economy.ads import ITEM_FOR
from ..economy.compute import MAX_NODES, NODE_SECONDS, promised_builder, subscription_cost
from ..economy.land import claimed, for_sale_at, plot_at, venue_room
from .observation import Observation
from .tools import TOOLS, ResidentTools

TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "land": ("buy_parcel", "joint_buy_parcel"),
    "compute": ("buy_node", "build_ad", "build_venue", "commission_build"),
    "content": ("make_content", "place_content"),
    "transport": ("build_portal", "link_portals", "teleport"),
    "exchange": ("place_order", "cancel_order"),
    "offers": (
        "offer_to_buy",
        "offer_to_sell",
        "offer_information",
        "offer_shares",
        "respond_to_offer",
        "withdraw_offer",
    ),
    "contracts": ("propose_contract", "deliver", "cancel_contract"),
    "memory": ("remember", "forget"),
}

DISCOVER_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "discover_tools",
        "description": (
            "Make additional game tools available in this round. Choose a group when the "
            "current tools cannot carry out your plan: land, compute, content, transport, "
            "exchange, offers, contracts or memory. The group's tools appear in the next request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "enum": list(TOOL_GROUPS),
                    "description": "The kind of tools to make available.",
                }
            },
            "required": ["group"],
            "additionalProperties": False,
        },
    },
}


def selected_tool_names(observation: Observation) -> set[str]:
    """Advertise actions useful in the observed state without hiding other actions forever."""
    me = observation.me
    names = {"say", "remember"}
    if observation.long_term_notes or observation.short_term_notes:
        names.add("forget")
    if observation.exchange.my_orders:
        names.add("cancel_order")
    # Compute and content work from anywhere, so these do not wait for the resident to stop
    # walking. Anyone may make content, to sell it as much as to show it.
    compute = observation.compute
    names.add("make_content")
    if any(
        parcel.facility is None
        and parcel.building_ends_in is None
        and parcel.covered_by is None
        and parcel.promised_to is None
        for parcel in observation.land.mine
    ) or any(
        permit.builder_id == me.id and permit.kind == "ad" for permit in observation.build_permits
    ):
        names.add("build_ad")
    if any(
        (parcel.venue_room or parcel.mall_orientations) and parcel.promised_to is None
        for parcel in observation.land.mine
    ) or any(
        permit.builder_id == me.id and permit.kind != "ad" for permit in observation.build_permits
    ):
        names.add("build_venue")
    if (
        len(compute.my_nodes) < compute.max_nodes
        and me.coins >= compute.node_seconds[0] * compute.node_coins_per_second
    ):
        names.add("buy_node")
    transport = observation.transport
    if transport is not None and (
        len(transport.mine) >= 2 or (transport.mine and transport.others)
    ):
        names.add("link_portals")
    if any(
        parcel.facility in ITEM_FOR
        and me.items.get(ITEM_FOR[parcel.facility])
        # A full game hall takes a new game by dropping its oldest; a full ad slot takes none.
        and (
            parcel.facility == "game_hall"
            or len(parcel.content_goes_stale_in) < observation.land.slot_capacity
        )
        for parcel in observation.land.mine
    ):
        names.add("place_content")
    if me.walking_to is None:
        names.add("move_to")
        here = observation.land.here
        if here is not None and here.owner is None:
            names.add("joint_buy_parcel")
            if me.coins >= here.price:
                names.add("buy_parcel")
        if transport is not None and transport.here is not None and transport.here.lines:
            names.add("teleport")
        if any(not resident.left and resident.id != me.id for resident in observation.residents):
            names.add("commission_build")
        if (
            observation.offers_to_me
            or observation.information_offers_to_me
            or observation.share_offers_to_me
            or observation.joint_purchases_to_me
            or observation.link_proposals_to_me
            or observation.build_commissions_to_me
        ):
            names.add("respond_to_offer")
        if any(parcel.shares.get(me.id) for parcel in observation.land.mine) or (
            observation.land.stakes
        ):
            names.add("offer_shares")
        if observation.contract_proposals_to_me:
            names.add("respond_to_offer")
        if (
            observation.my_offers
            or observation.my_information_offers
            or observation.my_share_offers
            or observation.my_joint_purchases
            or observation.my_link_proposals
            or observation.my_build_commissions
        ):
            names.add("withdraw_offer")
        if observation.my_contract_proposals:
            names.add("withdraw_offer")
        if any(contract.my_role == "supplier" for contract in observation.my_contracts):
            names.update(("deliver", "cancel_contract"))
    return names


def last_request_tool_names(tools: ResidentTools) -> set[str]:
    """Select all potentially usable actions from the live world before the final request."""
    world = tools.game.world
    me = next(resident for resident in world.residents if resident.id == tools.resident_id)
    names = {"say", "remember"}
    if me.memory.long_term or me.memory.short_term:
        names.add("forget")
    if any(order.resident_id == me.id for order in world.orders.orders):
        names.add("cancel_order")
    book = world.compute
    taken = claimed(world)
    names.add("make_content")
    my_parcels = [
        parcel for parcel in world.parcels if parcel.owner_id == me.id or parcel.held_by(me.id)
    ]
    permits = [permit for permit in book.permits if permit.builder_id == me.id]
    promised = {permit.parcel_id for permit in book.permits}
    if any(
        (parcel.col, parcel.row) not in taken and parcel.id not in promised for parcel in my_parcels
    ) or any(permit.kind == "ad" for permit in permits):
        names.add("build_ad")
    if any(
        (
            venue_room(world, parcel, taken, resident_id=me.id)
            and promised_builder(world, parcel.id, "game_hall") is None
        )
        or any(
            venue_room(world, parcel, taken, "mall", runs, me.id)
            and promised_builder(world, parcel.id, "mall", runs) is None
            for runs in ORIENTATIONS
        )
        for parcel in my_parcels
    ) or any(permit.kind != "ad" for permit in permits):
        names.add("build_venue")
    if sum(node.owner_id == me.id for node in book.nodes) < MAX_NODES and me.coins >= (
        subscription_cost(NODE_SECONDS[0])
    ):
        names.add("buy_node")
    if any(
        (parcel.owner_id == me.id or parcel.held_by(me.id) > 0)
        and parcel.facility is not None
        and parcel.facility.kind in ITEM_FOR
        and me.inventory.count(ITEM_FOR[parcel.facility.kind])
        for parcel in world.parcels
    ):
        names.add("place_content")
    portals = [
        parcel
        for parcel in world.parcels
        if parcel.facility is not None and parcel.facility.kind == "portal"
    ]
    mine = [parcel for parcel in portals if parcel.owner_id == me.id]
    gone = {resident.id for resident in world.residents if resident.left}
    joinable = [p for p in portals if p.owner_id != me.id and p.owner_id not in gone]
    if len(mine) >= 2 or (mine and joinable):
        names.add("link_portals")
    if me.busy:
        return names

    names.update(("move_to", "place_order"))
    square = for_sale_at(world, me.position)
    if square is not None:
        names.add("joint_buy_parcel")
        if me.coins >= square.price:
            names.add("buy_parcel")
    if any(resident.id != me.id and not resident.left for resident in world.residents):
        names.update(
            (
                "offer_to_buy",
                "offer_to_sell",
                "offer_information",
                "propose_contract",
                "commission_build",
            )
        )
    if any(parcel.held_by(me.id) for parcel in world.parcels):
        names.add("offer_shares")
    here = plot_at(world, me.position).id
    if any(link.joins(here) for link in world.links):
        names.add("teleport")
    if any(event.recipient_id == me.id for event in world.events.pending):
        names.add("respond_to_offer")
    if any(event.sender_id == me.id for event in world.events.pending):
        names.add("withdraw_offer")
    if any(contract.supplier_id == me.id for contract in world.contracts.active):
        names.update(("deliver", "cancel_contract"))
    return names


def selected_tool_specs(names: set[str], *, include_discovery: bool = True) -> list[dict[str, Any]]:
    """Return the requested real schemas and optionally the small discovery entry point."""
    specs = [spec.schema() for spec in TOOLS if spec.name in names]
    return specs + [DISCOVER_TOOL_SPEC] if include_discovery else specs


def discover_tools(arguments: str | dict[str, Any], names: set[str]) -> str:
    """Expand a group for subsequent requests in this round, without touching game state."""
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or set(parsed) != {"group"}:
        return json.dumps({"ok": False, "code": "invalid_arguments"})
    group = parsed["group"]
    if not isinstance(group, str) or group not in TOOL_GROUPS:
        return json.dumps({"ok": False, "code": "unknown_group"})
    expanded = TOOL_GROUPS[group]
    names.update(expanded)
    return json.dumps({"ok": True, "group": group, "enabled": expanded}, separators=(",", ":"))
