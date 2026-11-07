"""Choose the function schemas a resident needs for the current decision round.

The game still validates and executes every action through ``ResidentTools``. Discovery
only changes which schemas are advertised to the model; it does not grant capabilities.
"""

from __future__ import annotations

import json
from typing import Any

from ..economy.collectors import COLLECTOR_PRICE
from ..economy.compute import COMPUTE
from ..economy.facilities import AD_MATERIALS
from ..economy.gathering import DATA
from ..economy.land import plot_at
from ..economy.programs import PROGRAM_TIERS
from ..economy.studio import MATERIAL
from .observation import Observation
from .tools import TOOLS, ResidentTools

TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "production": (
        "buy_program",
        "sell_compute",
        "harvest",
        "gather_data",
        "write_program",
        "make_material",
    ),
    "land": ("buy_parcel", "build_ad"),
    "exchange": ("place_order", "cancel_order"),
    "offers": (
        "offer_to_buy",
        "offer_to_sell",
        "offer_information",
        "respond_to_offer",
        "withdraw_offer",
    ),
    "contracts": ("propose_contract", "deliver", "cancel_contract"),
    "collectors": ("build_collector", "install_program", "fund_collector", "remove_program"),
    "memory": ("remember", "forget"),
}

DISCOVER_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "discover_tools",
        "description": (
            "Make additional game tools available in this round. Choose a group when the "
            "current tools cannot carry out your plan: production, land, exchange, offers, "
            "contracts, collectors or memory. The group's tools appear in the next request."
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
    busy = any(
        (
            me.walking_to,
            me.harvesting_spring,
            me.gathering_mine,
            me.writing_program,
        )
    )
    names = {"say", "remember"}
    if observation.long_term_notes or observation.short_term_notes:
        names.add("forget")
    if observation.exchange.my_orders:
        names.add("cancel_order")
    if not busy:
        names.add("move_to")
        if me.at_market:
            names.add("buy_program")
            if me.compute > 1:
                names.add("sell_compute")
        if me.at_spring is not None:
            spring = next((s for s in observation.known_springs if s.id == me.at_spring), None)
            if me.programs:
                names.add("install_program")
                if spring is not None and spring.reserve > 0:
                    names.add("harvest")
            if (
                spring is not None
                and spring.reserve > 0
                and me.coins >= observation.collector_rules["price"]
            ):
                names.add("build_collector")
        if me.at_mine is not None:
            mine = next((q for q in observation.mines if q.id == me.at_mine), None)
            if mine is not None and mine.reserve > 0:
                names.add("gather_data")
        if me.at_studio and me.data > 0:
            names.update(("write_program", "make_material"))
        here = observation.land.here
        if here is not None:
            if here.owner is None and here.ad_income is not None and me.coins >= here.price:
                names.add("buy_parcel")
            if (
                here.owner == me.id
                and here.facility is None
                and me.materials >= observation.land.rules["ad_materials"]
            ):
                names.add("build_ad")
        if observation.offers_to_me or observation.information_offers_to_me:
            names.add("respond_to_offer")
        if observation.contract_proposals_to_me:
            names.add("respond_to_offer")
        if observation.my_offers or observation.my_information_offers:
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
    if any((me.walking, me.harvesting, me.gathering, me.writing)):
        return names

    names.update(("move_to", "place_order"))
    if world.market is not None and world.market.reaches(me.position):
        names.add("buy_program")
        if me.inventory.count(COMPUTE) > 1:
            names.add("sell_compute")
    spring = next((spring for spring in world.springs if spring.reaches(me.position)), None)
    if spring is not None:
        has_program = any(me.inventory.count(program.tier) > 0 for program in PROGRAM_TIERS)
        if has_program:
            names.add("install_program")
            if spring.reserve > 0:
                names.add("harvest")
        if spring.reserve > 0 and me.coins >= COLLECTOR_PRICE:
            names.add("build_collector")
    if any(mine.reaches(me.position) and mine.reserve > 0 for mine in world.mines):
        names.add("gather_data")
    if (
        world.studio is not None
        and world.studio.reaches(me.position)
        and me.inventory.count(DATA) > 0
    ):
        names.update(("write_program", "make_material"))
    square = plot_at(world, me.position)
    if not square.reserved and square.owner_id is None and me.coins >= square.price:
        names.add("buy_parcel")
    parcel = next((p for p in world.parcels if (p.col, p.row) == (square.col, square.row)), None)
    if (
        parcel is not None
        and parcel.owner_id == me.id
        and parcel.facility is None
        and me.inventory.count(MATERIAL) >= AD_MATERIALS
    ):
        names.add("build_ad")
    if any(resident.id != me.id and not resident.left for resident in world.residents):
        names.update(("offer_to_buy", "offer_to_sell", "offer_information", "propose_contract"))
    if any(event.recipient_id == me.id for event in world.events.pending):
        names.add("respond_to_offer")
    if any(event.sender_id == me.id for event in world.events.pending):
        names.add("withdraw_offer")
    if any(contract.supplier_id == me.id for contract in world.contracts.active):
        names.update(("deliver", "cancel_contract"))
    if any(slot.resident_id == me.id for collector in world.collectors for slot in collector.slots):
        names.update(("fund_collector", "remove_program"))
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
