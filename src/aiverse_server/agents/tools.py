"""Tools an agent acts through, bound to the one resident it drives.

None of the tools names who acts: `ResidentTools` holds the resident, so an agent can only
ever move its own, as change 0015 in the client repository asks. Each tool is a primitive
from `actions.py` performed and kept through `Game.commit`, as `Game.act` does, or a note
through `Game.remember` and `Game.forget`, so agents and pages go through the same rules and
broadcasts.

Leaving the game is not a tool: it gains a resident nothing, and a model could only do it by
mistake.

A tool's arguments come from a pydantic model, which both checks what a model sends and
gives the JSON Schema a model is shown (`tool_specs`). `call` never raises for anything a
model sends: unknown tools, malformed arguments and rejections all come back as a failed
`ToolResult`, and the world stays as it was.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from ..core.compute import JobSource, NodeKind
from ..core.land import AD_SLOT_CAPACITY, MAX_TOLL, PARCEL_SHARES
from ..core.world import NOTE_LENGTH_LIMIT, Position, Resident, World
from ..economy.ads import CONTENT_ITEM, CONTENT_LIFETIME, GAME_ITEM
from ..economy.compute import (
    AD_WORKLOAD,
    CONTENT_WORKLOAD,
    GAME_WORKLOAD,
    MAX_NODES,
    NODE_RATE,
    NODE_SECONDS,
    ON_DEMAND_PRICE,
    PORTAL_WORKLOAD,
    SELF_RATE,
    SUBSCRIPTION_PRICE,
    VENUE_WORKLOAD,
    job_seconds,
    on_demand_cost,
)
from ..economy.contracts import MAX_CONTRACT_SECONDS, MAX_DELIVERIES
from ..economy.exchange import DEFAULT_LIFETIME, LIFETIME_RANGE, MAX_PRICE, MAX_QUANTITY
from ..economy.portals import MAX_LINKS_PER_PORTAL, MAX_PORTALS, MIN_LINK_DISTANCE
from ..economy.sponsors import PAY_PERIOD
from ..game.actions import (
    Action,
    ActionRejected,
    BuildAd,
    BuildPortal,
    BuildVenue,
    BuyNode,
    BuyParcel,
    CancelContract,
    CancelOrder,
    Deliver,
    LinkPortals,
    MakeContent,
    MoveTo,
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
from ..game.lifecycle import GameError, total_coins
from ..residents.information_offer import SUMMARY_LIMIT
from ..residents.memory import NoteError
from ..residents.walking import WALK_SPEED

if TYPE_CHECKING:
    from ..server.app import Game

# What a failed call reports: a rejection code from the game, or one of these.
CALL_ERRORS = ("unknown_tool", "invalid_arguments")


class _Args(BaseModel):
    # Strict, as the protocol is: a boolean is not a number and "30" is not 30.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Coordinate = Annotated[float, Field(allow_inf_nan=False)]


class MoveToArgs(_Args):
    x: Coordinate = Field(description="Target x, from 0 (west) to the map size (east).")
    y: Coordinate = Field(description="Target y, from 0 (north) to the map size (south).")


class BuyParcelArgs(_Args):
    pass


class JointBuyParcelArgs(_Args):
    partner_id: str = Field(description="Resident id you buy the parcel with.")
    partner_pays: StrictInt = Field(
        description="Coins the partner pays towards the parcel's price, from 0 up to the price; "
        "you pay the rest, which is set aside now."
    )
    partner_shares: StrictInt = Field(
        description=f"Shares of the parcel's {PARCEL_SHARES} the partner gets, 1 to "
        f"{PARCEL_SHARES - 1}; you keep the others."
    )


class BuyNodeArgs(_Args):
    kind: Literal["generative", "render"] = Field(
        default="generative",
        description="generative: builds facilities (build_ad, build_portal, build_venue); render: "
        "makes content (make_content).",
    )
    seconds: StrictInt = Field(
        description=f"How long the subscription lasts, {NODE_SECONDS[0]} to {NODE_SECONDS[1]} "
        f"seconds, paid now at {SUBSCRIPTION_PRICE} coins per second."
    )


class BuildAdArgs(_Args):
    parcel_id: str = Field(description="id of one of your parcels with no facility and no job.")
    source: Literal["self", "on_demand", "subscription"] = Field(
        description="self: your own compute, free; on_demand: a node hired for this job, "
        "paid now; subscription: one of your nodes, named in node_id."
    )
    node_id: StrictInt | None = Field(
        default=None,
        description="id of one of your idle nodes in compute.my_nodes; only with source "
        "subscription.",
    )


class BuildPortalArgs(_Args):
    parcel_id: str = Field(description="id of one of your parcels with no facility and no job.")
    source: Literal["self", "on_demand", "subscription"] = Field(
        description="self: your own compute, free; on_demand: a node hired for this job, "
        "paid now; subscription: one of your generative nodes, named in node_id."
    )
    node_id: StrictInt | None = Field(
        default=None,
        description="id of one of your idle generative nodes in compute.my_nodes; only with "
        "source subscription.",
    )


class BuildVenueArgs(_Args):
    kind: Literal["game_hall", "workspace"] = Field(description="Which venue to build.")
    parcel_id: str = Field(description="id of one of your parcels with no facility and no job.")
    source: Literal["self", "on_demand", "subscription"] = Field(
        description="self: your own compute, free; on_demand: a node hired for this job, "
        "paid now; subscription: one of your generative nodes, named in node_id."
    )
    node_id: StrictInt | None = Field(
        default=None,
        description="id of one of your idle generative nodes in compute.my_nodes; only with "
        "source subscription.",
    )


class LinkPortalsArgs(_Args):
    parcel_id: str = Field(description="id of one of your parcels with a portal.")
    other_parcel_id: str = Field(
        description="id of the parcel with the portal to join it to: yours, or another "
        "resident's, who is then asked."
    )
    toll: StrictInt = Field(
        description=f"Coins one trip along the line costs, 0 to {MAX_TOLL}, paid by whoever "
        "travels to the holders of both parcels' shares."
    )


class TeleportArgs(_Args):
    to_parcel_id: str = Field(
        description="id of the parcel at the other end of a line from the portal you stand in."
    )


class MakeContentArgs(_Args):
    content: Literal["ad", "game"] = Field(
        default="ad",
        description=f"ad: a piece of {CONTENT_ITEM!r} for an ad slot; game: a piece of "
        f"{GAME_ITEM!r} for a game hall.",
    )
    source: Literal["self", "on_demand", "subscription"] = Field(
        description="self: your own compute, free; on_demand: a node hired for this job, "
        "paid now; subscription: one of your render nodes, named in node_id."
    )
    node_id: StrictInt | None = Field(
        default=None,
        description="id of one of your idle render nodes in compute.my_nodes; only with source "
        "subscription.",
    )


class PlaceContentArgs(_Args):
    parcel_id: str = Field(
        description="id of one of your parcels with an ad slot, or a game hall for games."
    )


class SayArgs(_Args):
    text: str = Field(description="What to say, at most 100 characters.")
    to: str | None = Field(
        default=None,
        description="Resident id to whisper to wherever they are; leave out to speak aloud "
        "to everyone within earshot.",
    )


class OfferToBuyArgs(_Args):
    seller_id: str = Field(description="Resident id you buy from.")
    kind: str = Field(description="Kind of item.")
    quantity: StrictInt = Field(description="How many items.")
    price: StrictInt = Field(description="Coins for the whole lot, set aside until answered.")


class OfferToSellArgs(_Args):
    buyer_id: str = Field(description="Resident id you sell to.")
    kind: str = Field(description="Kind of item.")
    quantity: StrictInt = Field(description="How many items, set aside until answered.")
    price: StrictInt = Field(description="Coins for the whole lot.")


class OfferInformationArgs(_Args):
    buyer_id: str = Field(description="Resident id you sell the information to.")
    summary: str = Field(
        description=f"What the buyer sees before paying, at most {SUMMARY_LIMIT} characters. "
        "Say what the information is about, not the information itself."
    )
    content: str = Field(
        description="What the buyer gets once they pay; they keep it as a long-term note that "
        f"also names you and your summary, so with those it must fit in {NOTE_LENGTH_LIMIT} "
        "characters. Anything you know: a place, a price, a plan, a rumour."
    )
    price: StrictInt = Field(description="Coins for the information.")


class OfferSharesArgs(_Args):
    buyer_id: str = Field(description="Resident id you hand the shares to.")
    parcel_id: str = Field(
        description="id of a parcel you hold shares of, in land.mine or land.stakes."
    )
    shares: StrictInt = Field(
        description=f"How many of the parcel's {PARCEL_SHARES} shares, at most those you hold "
        "that are not already offered."
    )
    price: StrictInt = Field(
        description="Coins for all of them, paid by the buyer when they accept; 0 gives them away."
    )


class PlaceOrderArgs(_Args):
    side: Literal["buy", "sell"]
    item: str = Field(description="Kind of item.")
    quantity: StrictInt = Field(description=f"Units, 1 to {MAX_QUANTITY}.")
    price: StrictInt = Field(description=f"Coins per unit, 1 to {MAX_PRICE}.")
    expires_in: float = Field(
        default=DEFAULT_LIFETIME,
        ge=LIFETIME_RANGE[0],
        le=LIFETIME_RANGE[1],
        allow_inf_nan=False,
        description="Seconds the unfilled part rests on the book.",
    )


class CancelOrderArgs(_Args):
    order_id: StrictInt = Field(description="order_id of one of your orders in exchange.my_orders.")


class RespondToOfferArgs(_Args):
    event_id: StrictInt = Field(
        description="event_id of an offer in offers_to_me, information_offers_to_me, "
        "share_offers_to_me or joint_purchases_to_me, or a proposal in "
        "contract_proposals_to_me or link_proposals_to_me."
    )
    accept: StrictBool = Field(description="true to accept, false to reject.")


class WithdrawOfferArgs(_Args):
    event_id: StrictInt = Field(
        description="event_id of an offer in my_offers, my_information_offers, "
        "my_share_offers or my_joint_purchases, or a proposal in my_contract_proposals "
        "or my_link_proposals."
    )


class ProposeContractArgs(_Args):
    partner_id: str = Field(description="Resident id you propose the contract to.")
    my_role: Literal["supplier", "buyer"] = Field(
        description="supplier: you deliver the items and get paid, your deposits set aside "
        "now; buyer: you pay for each delivery, and all your payments are set aside now."
    )
    kind: str = Field(description="Kind of item delivered.")
    quantity: StrictInt = Field(description="Items in each delivery.")
    price: StrictInt = Field(description="Coins the buyer pays for each delivery.")
    deliveries: StrictInt = Field(
        description=f"How many deliveries the supplier owes before it ends, 1 to "
        f"{MAX_DELIVERIES}; 1 for a one-off."
    )
    duration_seconds: StrictInt = Field(
        description=f"Seconds the contract stays in force once accepted, 1 to "
        f"{MAX_CONTRACT_SECONDS}."
    )
    deposit: StrictInt = Field(
        default=0,
        description="Coins the supplier stakes on each delivery, set aside on signing and "
        "returned with each delivery; those for deliveries not made go to the buyer. 0, the "
        "default, for none.",
    )


class DeliverArgs(_Args):
    contract_id: StrictInt = Field(
        description="contract_id of a contract in my_contracts where you are the supplier."
    )


class CancelContractArgs(DeliverArgs):
    pass


class RememberArgs(_Args):
    text: str = Field(description="The note, at most 300 characters.")
    term: Literal["long_term", "short_term"] = Field(
        default="short_term",
        description="long_term lasts the whole game; short_term, the default, is cleared after "
        "120 seconds.",
    )


class ForgetArgs(_Args):
    note_id: StrictInt = Field(description="Id of one of your notes.")


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    code: str | None = None
    """Why it failed: a rejection code from the game, or one of `CALL_ERRORS`."""
    message: str = ""
    status: dict[str, Any] | None = None
    """The resident's state right after the call."""

    def to_json(self) -> str:
        shown: dict[str, Any] = {"ok": self.ok}
        if self.code is not None:
            shown["code"] = self.code
        if self.message:
            shown["message"] = self.message
        if self.status is not None:
            shown["status"] = self.status
        return json.dumps(shown, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class ResourceBalance:
    coins: int


def resource_balance(game: Game, resident_id: str) -> ResourceBalance:
    """Owned resources, including escrow; reserving coins is not a loss."""
    return ResourceBalance(total_coins(game.world, resident_id))


@dataclass(frozen=True)
class ToolCall:
    """One call an agent made, for the turn's record."""

    name: str
    arguments: Mapping[str, Any]
    result: ToolResult
    time: float = 0.0
    coins_change: int = 0
    timestamp: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]

    def schema(self) -> dict[str, Any]:
        """The tool in the OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _without_titles(self.args.model_json_schema()),
            },
        }


def _without_titles(schema: Any) -> Any:
    """The schema without the `title` pydantic derives from each class and field name, which
    repeats the name and only costs the model request tokens, every request."""
    if isinstance(schema, dict):
        return {
            key: _without_titles(value)
            for key, value in schema.items()
            if not (key == "title" and isinstance(value, str))
        }
    if isinstance(schema, list):
        return [_without_titles(value) for value in schema]
    return schema


TOOLS = (
    ToolSpec(
        "move_to",
        "Walk in a straight line to a point. It costs time only: you are busy until you "
        f"arrive, at {WALK_SPEED:g} map units per second.",
        MoveToArgs,
    ),
    ToolSpec(
        "buy_parcel",
        "Buy the parcel of land you stand in from the town at its price; it stays yours for the "
        "game.",
        BuyParcelArgs,
    ),
    ToolSpec(
        "joint_buy_parcel",
        "Propose to another resident buying the parcel you stand in together, when you cannot "
        "or would rather not pay it all. They pay partner_pays of its price and get "
        "partner_shares of its shares, which split its income; you pay the rest, set aside "
        "until they answer, and keep the other shares. The parcel is bought when they accept; "
        "if someone buys it first, accepting fails and you should withdraw. You alone build on "
        "it and put content in it, however the shares are split.",
        JointBuyParcelArgs,
    ),
    ToolSpec(
        "buy_node",
        "Subscribe to a compute node for a stretch of time, from anywhere, even while walking. "
        "A generative node builds facilities (build_ad, build_portal, build_venue), a render "
        "node makes content (make_content). It is yours until it runs out and does one job at "
        "a time at "
        f"{NODE_RATE:g} workload per second, {NODE_RATE / SELF_RATE:g} times your own compute; "
        f"you pay {SUBSCRIPTION_PRICE} coins per second whether it works or not, and nothing is "
        f"paid back. You may hold {MAX_NODES} nodes at once, and each works on its own job.",
        BuyNodeArgs,
    ),
    ToolSpec(
        "build_ad",
        "Start building an ad slot on one of your parcels, from anywhere, even while walking; "
        "you need not stay, it appears when the job ends. It takes "
        f"{AD_WORKLOAD:g} workload: {job_seconds(AD_WORKLOAD, 'self'):g} s on your own compute "
        f"(free, one job at a time), {job_seconds(AD_WORKLOAD, 'on_demand'):g} s on a node hired "
        f"for it ({ON_DEMAND_PRICE} coins per workload, {on_demand_cost(AD_WORKLOAD)} coins), "
        "or on one of your subscribed generative nodes (if the node runs out first, the rest of "
        f"the job carries on at your own speed, {SELF_RATE:g} workload per second). "
        "An ad slot earns only while it shows content.",
        BuildAdArgs,
    ),
    ToolSpec(
        "build_portal",
        "Start building a portal on one of your parcels, from anywhere, even while walking; it "
        f"appears when the job ends. Up to {MAX_PORTALS} portals, built or being built. It "
        f"takes {PORTAL_WORKLOAD:g} workload: {job_seconds(PORTAL_WORKLOAD, 'self'):g} s on "
        f"your own compute (free, one job at a time), "
        f"{job_seconds(PORTAL_WORKLOAD, 'on_demand'):g} s on a node hired for it "
        f"({on_demand_cost(PORTAL_WORKLOAD)} coins), or on one of your subscribed generative "
        "nodes. A portal earns nothing itself: it matters through its lines (link_portals) "
        "and trips (teleport).",
        BuildPortalArgs,
    ),
    ToolSpec(
        "build_venue",
        "Start building a game hall or a workspace on one of your parcels, from anywhere, even "
        "while walking; it appears when the job ends. It takes "
        f"{VENUE_WORKLOAD:g} workload: {job_seconds(VENUE_WORKLOAD, 'self'):g} s on your own "
        f"compute (free, one job at a time), {job_seconds(VENUE_WORKLOAD, 'on_demand'):g} s on a "
        f"node hired for it ({on_demand_cost(VENUE_WORKLOAD)} coins), or on one of your "
        "subscribed generative nodes. A game hall shows games (make_content with content "
        f"game, then place_content), up to {AD_SLOT_CAPACITY} for {CONTENT_LIFETIME:g} s each. "
        "Tourists on the working route pay a fee at the door of each, and go to a game hall only "
        "while it shows games; the fees are paid out to the holders at paydays.",
        BuildVenueArgs,
    ),
    ToolSpec(
        "link_portals",
        "Join one of your portals to another with a line, from anywhere. When both are yours "
        "the line is drawn at once; when the other belongs to someone else they are asked, and "
        "it is drawn if they accept. The portals must be at least "
        f"{MIN_LINK_DISTANCE:g} units apart and a portal ends at most {MAX_LINKS_PER_PORTAL} "
        "lines. A line lets residents teleport between the two portals for the toll. Visitors "
        "may also use it, free, when it shortens their walk to a slot.",
        LinkPortalsArgs,
    ),
    ToolSpec(
        "teleport",
        "Travel at once from the portal in the parcel you stand in to the portal at the other "
        "end of one of its lines, paying the line's toll to the holders of the shares of both "
        "parcels. No time is spent, and you cannot while walking.",
        TeleportArgs,
    ),
    ToolSpec(
        "make_content",
        "Render one piece of content, from anywhere, even while walking; it joins the items "
        "you carry when the job ends, and you may sell it or put it in a matching facility of "
        f"yours. An ad ({CONTENT_ITEM!r}, for an ad slot) takes {CONTENT_WORKLOAD:g} workload: "
        f"{job_seconds(CONTENT_WORKLOAD, 'self'):g} s on your own compute (free, one job at a "
        f"time), {job_seconds(CONTENT_WORKLOAD, 'on_demand'):g} s on a node hired for it "
        f"({on_demand_cost(CONTENT_WORKLOAD)} coins), or on one of your subscribed render nodes. "
        f"A game ({GAME_ITEM!r}, for a game hall) takes {GAME_WORKLOAD:g}: "
        f"{job_seconds(GAME_WORKLOAD, 'self'):g} s, {on_demand_cost(GAME_WORKLOAD)} coins hired.",
        MakeContentArgs,
    ),
    ToolSpec(
        "place_content",
        f"Put one piece of {CONTENT_ITEM!r} you carry into the ad slot on one of your parcels, "
        f"or of {GAME_ITEM!r} into a game hall, from anywhere. A facility shows up to "
        f"{AD_SLOT_CAPACITY} pieces; each is shown for {CONTENT_LIFETIME:g} s from now and then "
        f"goes stale. Every {PAY_PERIOD:g} s the sponsors pay an ad slot for the people who saw "
        "it, and more pieces draw more visitors; a game hall earns from the fees tourists on "
        "the working route pay at its door, and draws them only while it shows games.",
        PlaceContentArgs,
    ),
    ToolSpec(
        "say",
        "Say something aloud to residents nearby, or whisper to one resident anywhere. Free, "
        "and allowed while walking.",
        SayArgs,
    ),
    ToolSpec(
        "offer_to_buy",
        "Offer another resident coins for their items; your coins are set aside until they "
        "answer or you withdraw.",
        OfferToBuyArgs,
    ),
    ToolSpec(
        "offer_to_sell",
        "Offer another resident your items for coins; your items are set aside until they "
        "answer or you withdraw.",
        OfferToSellArgs,
    ),
    ToolSpec(
        "offer_information",
        "Offer another resident a copy of something you know for coins. They see only your "
        "summary until they accept and pay, then get the content as a note. Nothing is set "
        "aside and you keep the information, so you can sell it again. The server does not "
        "check that it is true.",
        OfferInformationArgs,
    ),
    ToolSpec(
        "offer_shares",
        f"Offer another resident shares of a block, out of its {PARCEL_SHARES}; name any parcel "
        "of the block and the same shares of all its parcels change hands. The holders of "
        "a block's shares split what its facilities earn in proportion, at every payday, "
        "whoever built them. Use it to pay for work or share a block with a partner. Nothing "
        "moves until they accept and pay, and you keep earning on the shares until then. "
        "Whoever bought the block still alone builds on it and stocks it with content.",
        OfferSharesArgs,
    ),
    ToolSpec(
        "respond_to_offer",
        "Accept or reject an offer (items, information, parcel shares or a joint purchase of "
        "land) or contract proposal sent to you.",
        RespondToOfferArgs,
    ),
    ToolSpec(
        "place_order",
        "Place an order on the exchange from anywhere. It trades at once with "
        "the best-priced orders on the other side (each at that order's price; a buyer gets "
        "back the difference) and the rest waits on the book. A buy order sets aside quantity "
        "x price coins, a sell order the items, until filled, cancelled or expired.",
        PlaceOrderArgs,
    ),
    ToolSpec(
        "cancel_order",
        "Cancel one of your resting orders from anywhere, even while busy; what it still holds "
        "comes back.",
        CancelOrderArgs,
    ),
    ToolSpec(
        "withdraw_offer",
        "Take back an offer or contract proposal you sent that is not answered.",
        WithdrawOfferArgs,
    ),
    ToolSpec(
        "propose_contract",
        "Propose a standing deal: the supplier owes a number of deliveries before it ends, and "
        "each one is paid for automatically from the buyer's coins set aside when the contract "
        "is signed. Comes into force when the partner accepts.",
        ProposeContractArgs,
    ),
    ToolSpec(
        "deliver",
        "As a contract's supplier, hand the buyer one delivery from what you carry and be "
        "paid its price at once, with its deposit back.",
        DeliverArgs,
    ),
    ToolSpec(
        "cancel_contract",
        "As a contract's supplier, call off its remaining deliveries; the buyer gets their "
        "set-aside coins back, and your deposit for those deliveries.",
        CancelContractArgs,
    ),
    ToolSpec(
        "remember",
        "Write a note to yourself; your notes are shown to you every round.",
        RememberArgs,
    ),
    ToolSpec("forget", "Remove one of your notes.", ForgetArgs),
)


def tool_specs() -> list[dict[str, Any]]:
    """Every tool, in the OpenAI function-calling format."""
    return [spec.schema() for spec in TOOLS]


@dataclass
class ResidentTools:
    """The tools of one resident in one game; keeps every call made through it."""

    game: Game
    resident_id: str
    calls: list[ToolCall] = field(default_factory=list)

    def call(self, name: str, arguments: Mapping[str, Any] | str) -> ToolResult:
        """Run a tool by name, as a model asks for it; arguments may be a JSON object text."""
        timestamp = datetime.now(UTC).isoformat()
        self.game.tick()
        if isinstance(arguments, str):
            try:
                loaded = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                # Malformed JSON, or an integer too long to convert.
                loaded = None
            if not isinstance(loaded, dict):
                result = ToolResult(False, "invalid_arguments", "arguments must be a JSON object")
                self.calls.append(
                    ToolCall(
                        name,
                        {"_raw": "[invalid JSON omitted]"},
                        result,
                        self.game.world.time,
                        timestamp=timestamp,
                    )
                )
                return result
            arguments = loaded
        # Settle timed work before measuring this action, so a payment that fell due meanwhile
        # is not credited to an unrelated tool call.
        before = resource_balance(self.game, self.resident_id)
        time = self.game.world.time
        result = self._dispatch(name, arguments)
        after = resource_balance(self.game, self.resident_id)
        self.calls.append(
            ToolCall(
                name,
                dict(arguments),
                result,
                time,
                after.coins - before.coins,
                timestamp,
            )
        )
        return result

    def move_to(self, x: float, y: float) -> ToolResult:
        return self.call("move_to", {"x": x, "y": y})

    def buy_parcel(self) -> ToolResult:
        return self.call("buy_parcel", {})

    def buy_node(self, seconds: int, kind: NodeKind = "generative") -> ToolResult:
        return self.call("buy_node", {"seconds": seconds, "kind": kind})

    def build_ad(self, parcel_id: str, source: JobSource, node_id: int | None = None) -> ToolResult:
        arguments: dict[str, Any] = {"parcel_id": parcel_id, "source": source}
        if node_id is not None:
            arguments["node_id"] = node_id
        return self.call("build_ad", arguments)

    def build_portal(
        self, parcel_id: str, source: JobSource, node_id: int | None = None
    ) -> ToolResult:
        arguments: dict[str, Any] = {"parcel_id": parcel_id, "source": source}
        if node_id is not None:
            arguments["node_id"] = node_id
        return self.call("build_portal", arguments)

    def build_venue(
        self,
        parcel_id: str,
        kind: Literal["game_hall", "workspace"],
        source: JobSource,
        node_id: int | None = None,
    ) -> ToolResult:
        arguments: dict[str, Any] = {"parcel_id": parcel_id, "kind": kind, "source": source}
        if node_id is not None:
            arguments["node_id"] = node_id
        return self.call("build_venue", arguments)

    def link_portals(self, parcel_id: str, other_parcel_id: str, toll: int) -> ToolResult:
        arguments = {"parcel_id": parcel_id, "other_parcel_id": other_parcel_id, "toll": toll}
        return self.call("link_portals", arguments)

    def teleport(self, to_parcel_id: str) -> ToolResult:
        return self.call("teleport", {"to_parcel_id": to_parcel_id})

    def make_content(
        self,
        source: JobSource,
        node_id: int | None = None,
        content: Literal["ad", "game"] = "ad",
    ) -> ToolResult:
        arguments: dict[str, Any] = {"source": source, "content": content}
        if node_id is not None:
            arguments["node_id"] = node_id
        return self.call("make_content", arguments)

    def place_content(self, parcel_id: str) -> ToolResult:
        return self.call("place_content", {"parcel_id": parcel_id})

    def place_order(
        self, side: str, item: str, quantity: int, price: int, expires_in: float = DEFAULT_LIFETIME
    ) -> ToolResult:
        arguments = {"side": side, "item": item, "quantity": quantity, "price": price}
        return self.call("place_order", {**arguments, "expires_in": expires_in})

    def cancel_order(self, order_id: int) -> ToolResult:
        return self.call("cancel_order", {"order_id": order_id})

    def say(self, text: str, to: str | None = None) -> ToolResult:
        return self.call("say", {"text": text} if to is None else {"text": text, "to": to})

    def propose_contract(
        self,
        partner_id: str,
        my_role: Literal["supplier", "buyer"],
        kind: str,
        quantity: int,
        price: int,
        deliveries: int,
        duration_seconds: int,
        deposit: int = 0,
    ) -> ToolResult:
        return self.call(
            "propose_contract",
            {
                "partner_id": partner_id,
                "my_role": my_role,
                "kind": kind,
                "quantity": quantity,
                "price": price,
                "deliveries": deliveries,
                "duration_seconds": duration_seconds,
                "deposit": deposit,
            },
        )

    def deliver(self, contract_id: int) -> ToolResult:
        return self.call("deliver", {"contract_id": contract_id})

    def _dispatch(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        spec = next((spec for spec in TOOLS if spec.name == name), None)
        if spec is None:
            return ToolResult(False, "unknown_tool", f"no tool named {name!r}"[:200])
        try:
            args = spec.args.model_validate(arguments)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc']) or 'arguments'}: {issue['msg']}"
                for issue in error.errors()
            )
            return ToolResult(False, "invalid_arguments", details[:300])
        try:
            message = self._run(args)
        except ActionRejected as rejection:
            return ToolResult(False, rejection.code, rejection.message, self.status())
        except (GameError, NoteError) as error:
            return ToolResult(False, error.code, error.message, self.status())
        return ToolResult(True, message=message, status=self.status())

    def _run(self, args: _Args) -> str:
        """Carry out a tool; returns what the model needs to know beyond its status."""
        me = self.resident_id
        act = self._act
        match args:
            case MoveToArgs():
                act(MoveTo(me, Position(args.x, args.y)))
            case BuyParcelArgs():
                act(BuyParcel(me))
                parcel = self.game.world.parcels[-1]
                return f"bought parcel {parcel.id}"
            case JointBuyParcelArgs():
                act(OfferJointPurchase(me, args.partner_id, args.partner_pays, args.partner_shares))
            case BuyNodeArgs():
                act(BuyNode(me, args.kind, args.seconds))
                node = self.game.world.compute.nodes[-1]
                return f"subscribed to {args.kind} node {node.id} for {args.seconds} s"
            case BuildAdArgs():
                act(BuildAd(me, args.parcel_id, args.source, args.node_id))
                job = self.game.world.compute.jobs[-1]
                seconds = job.ends_at - job.started_at
                return f"job {job.id}: ad slot on {job.parcel_id} ready in {seconds:g} s"
            case BuildPortalArgs():
                act(BuildPortal(me, args.parcel_id, args.source, args.node_id))
                job = self.game.world.compute.jobs[-1]
                seconds = job.ends_at - job.started_at
                return f"job {job.id}: portal on {job.parcel_id} ready in {seconds:g} s"
            case BuildVenueArgs():
                act(BuildVenue(me, args.parcel_id, args.kind, args.source, args.node_id))
                job = self.game.world.compute.jobs[-1]
                seconds = job.ends_at - job.started_at
                name = args.kind.replace("_", " ")
                return f"job {job.id}: {name} on {job.parcel_id} ready in {seconds:g} s"
            case LinkPortalsArgs():
                lines = len(self.game.world.links)
                act(LinkPortals(me, args.parcel_id, args.other_parcel_id, args.toll))
                if len(self.game.world.links) > lines:
                    return f"{args.parcel_id} and {args.other_parcel_id} are now linked"
                return f"asked the owner of {args.other_parcel_id}; the line waits for their answer"
            case TeleportArgs():
                act(Teleport(me, args.to_parcel_id))
                return f"arrived at {args.to_parcel_id}"
            case MakeContentArgs():
                act(MakeContent(me, args.source, args.node_id, args.content))
                job = self.game.world.compute.jobs[-1]
                seconds = job.ends_at - job.started_at
                what = "a piece of content" if args.content == "ad" else "a game"
                return f"job {job.id}: {what} ready in {seconds:g} s"
            case PlaceContentArgs():
                act(PlaceContent(me, args.parcel_id))
                parcel = next(p for p in self.game.world.parcels if p.id == args.parcel_id)
                shown = len(parcel.facility.contents) if parcel.facility else 0
                hall = parcel.facility is not None and parcel.facility.kind == "game_hall"
                where = "game hall" if hall else "ad slot"
                return f"the {where} on {args.parcel_id} now shows {shown}"
            case SayArgs():
                act(Say(me, args.text, args.to))
            case OfferToBuyArgs():
                act(OfferToBuy(me, args.seller_id, args.kind, args.quantity, args.price))
            case OfferToSellArgs():
                act(OfferToSell(me, args.buyer_id, args.kind, args.quantity, args.price))
            case OfferInformationArgs():
                act(OfferInformation(me, args.buyer_id, args.summary, args.content, args.price))
            case OfferSharesArgs():
                act(OfferShares(me, args.buyer_id, args.parcel_id, args.shares, args.price))
            case PlaceOrderArgs():
                book = self.game.world.orders
                order_id, first_fill = book.next_id, book.next_seq
                act(
                    PlaceOrder(me, args.side, args.item, args.quantity, args.price, args.expires_in)
                )
                return _order_outcome(self.game.world, order_id, first_fill)
            case CancelOrderArgs():
                act(CancelOrder(me, args.order_id))
            case RespondToOfferArgs():
                act(RespondToEvent(me, args.event_id, args.accept))
            case WithdrawOfferArgs():
                act(WithdrawEvent(me, args.event_id))
            case ProposeContractArgs():
                act(
                    ProposeContract(
                        me,
                        args.partner_id,
                        args.my_role == "supplier",
                        args.kind,
                        args.quantity,
                        args.price,
                        args.deliveries,
                        args.duration_seconds,
                        args.deposit,
                    )
                )
            # The subclass first: a cancellation would match the delivery's arguments too.
            case CancelContractArgs():
                act(CancelContract(me, args.contract_id))
            case DeliverArgs():
                act(Deliver(me, args.contract_id))
            case RememberArgs():
                self.game.remember(me, args.text, args.term)
            case ForgetArgs():
                self.game.forget(me, args.note_id)
            case _:
                raise AssertionError(f"tool arguments without a handler: {type(args).__name__}")
        return ""

    def _act(self, action: Action) -> None:
        """Carry out a primitive as `Game.act` does. Raises `ActionRejected`, leaving the world
        as it was."""
        self.game.tick()
        self.game.commit(perform(self.game.world, action))

    def status(self) -> dict[str, Any]:
        """A short summary of the resident right now, handed back after every call."""
        world = self.game.world
        me = _resident(world, self.resident_id)
        shown: dict[str, Any] = {"coins": me.coins, "items": me.inventory.counts()}
        if me.walking is not None:
            target = me.walking.target
            shown["walking_to"] = {"x": round(target.x, 1), "y": round(target.y, 1)}
            shown["arrives_in"] = round(max(me.walking.arrives_at - world.time, 0.0), 1)
        if me.left:
            shown["left_game"] = True
        if world.ended:
            shown["game_over"] = True
        return shown


def _order_outcome(world: World, order_id: int, first_fill: int) -> str:
    """What became of a newly placed order: its fills and what rests on the book."""
    fills = [fill for fill in world.orders.fills if fill.seq >= first_fill]
    filled = sum(fill.quantity for fill in fills)
    resting = next((order for order in world.orders.orders if order.id == order_id), None)
    parts = []
    if fills:
        coins = sum(fill.quantity * fill.price for fill in fills)
        parts.append(f"filled {filled} for {coins} coins")
    if resting is not None:
        parts.append(f"order {order_id}: {resting.quantity} resting at {resting.price} each")
    return "; ".join(parts)


def _resident(world: World, resident_id: str) -> Resident:
    return next(resident for resident in world.residents if resident.id == resident_id)
