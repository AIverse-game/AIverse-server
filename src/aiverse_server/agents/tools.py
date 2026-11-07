"""Tools an agent acts through, bound to the one resident it drives.

None of the tools names who acts: `ResidentTools` holds the resident, so an agent can only
ever move its own, as change 0015 in the client repository asks. Each tool is a primitive
from `actions.py` performed and kept through `Game.commit`, as `Game.act` does, or a note
through `Game.remember` and `Game.forget`, so agents and pages go through the same rules and
broadcasts.

Leaving the game is not a tool: it gains a resident nothing, and a model could only do it by
mistake. For the same reason no tool spends a resident's last compute, which would take them
out as well: a call that would leave them none in hand is refused with `last_compute`.

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

from ..core.inventory import item_count
from ..core.world import NOTE_LENGTH_LIMIT, Position, Resident, World
from ..economy.compute import COMPUTE, COMPUTE_PER_UNIT, compute_total
from ..economy.contracts import MAX_CONTRACT_SECONDS, MAX_DELIVERIES
from ..economy.exchange import DEFAULT_LIFETIME, LIFETIME_RANGE, MAX_PRICE, MAX_QUANTITY
from ..economy.facilities import (
    AD_MATERIALS,
    FACILITY_SECONDS,
    HOST_RADIUS,
    MEETING_COMPUTE,
    MEETING_MATERIALS,
    MEETING_PROGRAM,
    SERVER_CAPACITY,
    SERVER_MATERIALS,
    ad_income,
    host_for,
    hosting_fee,
    meeting_income,
)
from ..economy.land import plot_at
from ..economy.market import pressure_at
from ..economy.studio import MATERIAL_RECIPE
from ..game.actions import (
    Action,
    ActionRejected,
    BuildAd,
    BuildCollector,
    BuildMeetingRoom,
    BuildServer,
    BuyParcel,
    BuyProgram,
    CancelContract,
    CancelOrder,
    Deliver,
    FundCollector,
    GatherData,
    Harvest,
    InstallProgram,
    MakeMaterial,
    MoveTo,
    OfferInformation,
    OfferToBuy,
    OfferToSell,
    PlaceOrder,
    ProposeContract,
    RemoveProgram,
    RespondToEvent,
    Say,
    SellCompute,
    SetHostingFee,
    SupplyServer,
    WithdrawEvent,
    WriteProgram,
    perform,
)
from ..game.lifecycle import GameError, total_coins
from ..residents.information_offer import SUMMARY_LIMIT
from ..residents.memory import NoteError
from ..residents.perception import springs_in_sight
from .observation import known_slots

if TYPE_CHECKING:
    from ..server.app import Game

PRICE_HOLD_SECONDS = 30.0
"""How long after a round began a resident still sells at the price they saw then. Others'
sales in that time do not lower it; later ones do, so a long round cannot sell at a price the
market left long ago."""

# What a failed call reports: a rejection code from the game, or one of these.
CALL_ERRORS = ("unknown_tool", "invalid_arguments", "last_compute")


class _LastCompute(Exception):
    """A primitive would have left the resident no compute, taking them out of the game."""


class _Args(BaseModel):
    # Strict, as the protocol is: a boolean is not a number and "30" is not 30.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Coordinate = Annotated[float, Field(allow_inf_nan=False)]


class MoveToArgs(_Args):
    x: Coordinate = Field(description="Target x, from 0 (west) to the map size (east).")
    y: Coordinate = Field(description="Target y, from 0 (north) to the map size (south).")


class HarvestArgs(_Args):
    spring_id: str = Field(description="Id of the spring you stand at, such as s1.")


class GatherDataArgs(_Args):
    mine_id: str = Field(description="Id of the mine you stand at, such as m1.")


class WriteProgramArgs(_Args):
    tier: str = Field(description="Program tier to write: v1, v2 or v3.")


class MakeMaterialArgs(_Args):
    pass


class BuyParcelArgs(_Args):
    pass


class BuildAdArgs(_Args):
    pass


class BuildServerArgs(_Args):
    fee: StrictInt = Field(
        description="Coins each hosted meeting room of another owner pays you per cycle; >= 0."
    )


class BuildMeetingRoomArgs(_Args):
    pass


class SupplyServerArgs(_Args):
    amount: StrictInt = Field(description="Carried compute to put into the server; at least 1.")


class SetHostingFeeArgs(_Args):
    server: str = Field(description="Parcel id of your server, such as L7-3.")
    fee: StrictInt = Field(description="New fee in coins per hosted cycle; >= 0.")


class SellComputeArgs(_Args):
    quantity: StrictInt = Field(description="Compute to sell; you must stand at the market.")


class BuyProgramArgs(_Args):
    tier: str = Field(description="Program tier: v1, v2 or v3.")


class BuildCollectorArgs(_Args):
    spring_id: str = Field(description="Spring you stand at; build an automatic collector here.")


class InstallProgramArgs(_Args):
    collector_id: StrictInt
    program: str = Field(description="Carried program tier to install: v1, v2 or v3.")
    deposit: StrictInt = Field(description="Coins to prepay for rent and upkeep; at least 1.")


class FundCollectorArgs(_Args):
    collector_id: StrictInt
    slot_id: StrictInt
    amount: StrictInt = Field(description="Additional prepaid coins for your slot; at least 1.")


class RemoveProgramArgs(_Args):
    collector_id: StrictInt
    slot_id: StrictInt


class SayArgs(_Args):
    text: str = Field(description="What to say, at most 100 characters.")
    to: str | None = Field(
        default=None,
        description="Resident id to whisper to wherever they are; leave out to speak aloud "
        "to everyone within earshot.",
    )


class OfferToBuyArgs(_Args):
    seller_id: str = Field(description="Resident id you buy from.")
    kind: str = Field(description='Item: "compute", "data", "material" or a program like "v1".')
    quantity: StrictInt = Field(description="How many items.")
    price: StrictInt = Field(description="Coins for the whole lot, set aside until answered.")


class OfferToSellArgs(_Args):
    buyer_id: str = Field(description="Resident id you sell to.")
    kind: str = Field(description='Item: "compute", "data", "material" or a program like "v1".')
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


class PlaceOrderArgs(_Args):
    side: Literal["buy", "sell"]
    item: str = Field(description='Item: "compute", "data", "material" or a program like "v1".')
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
        description="event_id of an offer in offers_to_me or information_offers_to_me, or a "
        "proposal in contract_proposals_to_me."
    )
    accept: StrictBool = Field(description="true to accept, false to reject.")


class WithdrawOfferArgs(_Args):
    event_id: StrictInt = Field(
        description="event_id of an offer in my_offers or my_information_offers, or a proposal "
        "in my_contract_proposals."
    )


class ProposeContractArgs(_Args):
    partner_id: str = Field(description="Resident id you propose the contract to.")
    my_role: Literal["supplier", "buyer"] = Field(
        description="supplier: you deliver the items and get paid, your deposits set aside "
        "now; buyer: you pay for each delivery, and all your payments are set aside now."
    )
    kind: str = Field(
        description='Item kind delivered: "compute", "data", "material" or a program like "v1".'
    )
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
    compute: int


def resource_balance(game: Game, resident_id: str) -> ResourceBalance:
    """Owned resources, including escrow; reserving coins is not a loss."""
    return ResourceBalance(
        total_coins(game.world, resident_id), compute_total(game.world, resident_id)
    )


@dataclass(frozen=True)
class ToolCall:
    """One call an agent made, for the turn's record."""

    name: str
    arguments: Mapping[str, Any]
    result: ToolResult
    time: float = 0.0
    coins_change: int = 0
    compute_change: int = 0
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
        "build_collector",
        "Build a collector at your spring for coins; it needs installed programs.",
        BuildCollectorArgs,
    ),
    ToolSpec(
        "install_program",
        "Install your program at a collector you stand at; prepay rent and upkeep, then move "
        "freely.",
        InstallProgramArgs,
    ),
    ToolSpec(
        "fund_collector",
        "Remotely top up your slot. An unfunded slot pauses without debt or catch-up.",
        FundCollectorArgs,
    ),
    ToolSpec(
        "remove_program",
        "Remotely remove your program and refund unused prepayment.",
        RemoveProgramArgs,
    ),
    ToolSpec(
        "move_to",
        "Walk in a straight line to a point. Costs "
        f"{COMPUTE_PER_UNIT:g} compute per map unit, rounded up per walk, paid on setting off; "
        "you are busy until you arrive.",
        MoveToArgs,
    ),
    ToolSpec(
        "harvest",
        "Harvest the spring you stand at with the best program you carry; the compute arrives "
        "when the job ends and you are busy until then.",
        HarvestArgs,
    ),
    ToolSpec(
        "gather_data",
        "Gather data at the mine you stand at; no program needed. The data arrives when the "
        "job ends and you are busy until then.",
        GatherDataArgs,
    ),
    ToolSpec(
        "write_program",
        "Write one program at the studio you stand at: its recipe's data, compute and, above "
        "v1, one program of the tier below are spent at once; the program arrives when the job "
        "ends and you are busy until then.",
        WriteProgramArgs,
    ),
    ToolSpec(
        "make_material",
        f"Make {MATERIAL_RECIPE.count} materials at the studio you stand at from "
        f"{MATERIAL_RECIPE.data} data and {MATERIAL_RECIPE.compute} compute, spent at once; they "
        "arrive when the job ends and you are busy until then. Facilities are built of materials.",
        MakeMaterialArgs,
    ),
    ToolSpec(
        "buy_parcel",
        "Buy the parcel of land you stand in from the town at its price; it stays yours for the "
        "game. Parcels touching a spring, mine, the studio or the market cannot be bought.",
        BuyParcelArgs,
    ),
    ToolSpec(
        "build_ad",
        f"Build an ad slot on your own parcel you stand in, using up {AD_MATERIALS} carried "
        f"materials. Every {FACILITY_SECONDS:g}s it pays you coins for the visitors passing by.",
        BuildAdArgs,
    ),
    ToolSpec(
        "build_server",
        f"Build a server on your own bare parcel you stand in, using up {SERVER_MATERIALS} "
        f"carried materials. Supplied with compute, it hosts meeting rooms within "
        f"{HOST_RADIUS:g} units, spending {MEETING_COMPUTE} compute per room each cycle; other "
        "owners' rooms pay you the fee out of their income.",
        BuildServerArgs,
    ),
    ToolSpec(
        "build_meeting_room",
        f"Build a meeting room on your own bare parcel you stand in, using up "
        f"{MEETING_MATERIALS} carried materials and a {MEETING_PROGRAM} program. Every "
        f"{FACILITY_SECONDS:g}s a server in reach hosts it, it pays you for visitors minus the "
        "server's fee; with no server able to host, it earns nothing that cycle.",
        BuildMeetingRoomArgs,
    ),
    ToolSpec(
        "supply_server",
        f"Put carried compute into the server on the parcel you stand in, anyone's, up to "
        f"{SERVER_CAPACITY} held. It is spent hosting meeting rooms and never comes back.",
        SupplyServerArgs,
    ),
    ToolSpec(
        "set_hosting_fee",
        "Change the fee one of your servers charges other owners' meeting rooms, from anywhere. "
        "A room uses its owner's own server first, then the cheapest in reach.",
        SetHostingFeeArgs,
    ),
    ToolSpec(
        "sell_compute",
        "Sell carried compute to the market for coins at the market price, which falls the more "
        f"is sold. Others' sales in the {PRICE_HOLD_SECONDS:g} seconds after your round began do "
        "not lower it. Keep at least 1 compute: selling your last is refused.",
        SellComputeArgs,
    ),
    ToolSpec(
        "buy_program", "Buy one program at the market for its fixed price in coins.", BuyProgramArgs
    ),
    ToolSpec(
        "say",
        "Say something aloud to residents nearby, or whisper to one resident anywhere. Costs "
        "compute.",
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
        "respond_to_offer",
        "Accept or reject an offer (items or information) or contract proposal sent to you.",
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
    seen_pressure: float | None = None
    """Market pressure when the round began, plus the resident's own sales since: sales within
    `PRICE_HOLD_SECONDS` of `seen_at` are priced at the price the resident saw then, or the
    current one if higher, see `market.quote`."""
    seen_at: float = 0.0
    """Game time when the round began."""

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
        # Settle timed work before measuring this action, so a completed harvest is not
        # credited to an unrelated tool call.
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
                after.compute - before.compute,
                timestamp,
            )
        )
        return result

    def move_to(self, x: float, y: float) -> ToolResult:
        return self.call("move_to", {"x": x, "y": y})

    def harvest(self, spring_id: str) -> ToolResult:
        return self.call("harvest", {"spring_id": spring_id})

    def sell_compute(self, quantity: int) -> ToolResult:
        return self.call("sell_compute", {"quantity": quantity})

    def gather_data(self, mine_id: str) -> ToolResult:
        return self.call("gather_data", {"mine_id": mine_id})

    def write_program(self, tier: str) -> ToolResult:
        return self.call("write_program", {"tier": tier})

    def make_material(self) -> ToolResult:
        return self.call("make_material", {})

    def buy_parcel(self) -> ToolResult:
        return self.call("buy_parcel", {})

    def build_ad(self) -> ToolResult:
        return self.call("build_ad", {})

    def build_server(self, fee: int) -> ToolResult:
        return self.call("build_server", {"fee": fee})

    def build_meeting_room(self) -> ToolResult:
        return self.call("build_meeting_room", {})

    def supply_server(self, amount: int) -> ToolResult:
        return self.call("supply_server", {"amount": amount})

    def set_hosting_fee(self, server: str, fee: int) -> ToolResult:
        return self.call("set_hosting_fee", {"server": server, "fee": fee})

    def place_order(
        self, side: str, item: str, quantity: int, price: int, expires_in: float = DEFAULT_LIFETIME
    ) -> ToolResult:
        arguments = {"side": side, "item": item, "quantity": quantity, "price": price}
        return self.call("place_order", {**arguments, "expires_in": expires_in})

    def cancel_order(self, order_id: int) -> ToolResult:
        return self.call("cancel_order", {"order_id": order_id})

    def buy_program(self, tier: str) -> ToolResult:
        return self.call("buy_program", {"tier": tier})

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
        except _LastCompute:
            return ToolResult(
                False,
                "last_compute",
                "that would leave you no compute in hand (compute set aside in offers or sell "
                "orders does not count) and could take you out of the game; keep at least 1 "
                "compute",
                self.status(),
            )
        except (GameError, NoteError) as error:
            return ToolResult(False, error.code, error.message, self.status())
        return ToolResult(True, message=message, status=self.status())

    def _run(self, args: _Args) -> str:
        """Carry out a tool; returns what the model needs to know beyond its status."""
        me = self.resident_id
        act = self._act
        match args:
            case BuildCollectorArgs():
                self._require_known_spring(args.spring_id)
                act(BuildCollector(me, args.spring_id))
                # Collectors are numbered as they are built; the model needs the number to install.
                collector_id = self.game.world.next_collector_id - 1
                return (
                    f"built collector {collector_id}; install programs into it with "
                    f"collector_id {collector_id}"
                )
            case InstallProgramArgs():
                self._require_known_collector(args.collector_id)
                act(InstallProgram(me, args.collector_id, args.program, args.deposit))
            case FundCollectorArgs():
                self._require_known_collector(args.collector_id)
                act(FundCollector(me, args.collector_id, args.slot_id, args.amount))
            case RemoveProgramArgs():
                self._require_known_collector(args.collector_id)
                act(RemoveProgram(me, args.collector_id, args.slot_id))
            case MoveToArgs():
                act(MoveTo(me, Position(args.x, args.y)))
            case HarvestArgs():
                self._require_known_spring(args.spring_id)
                act(Harvest(me, args.spring_id))
            case GatherDataArgs():
                act(GatherData(me, args.mine_id))
            case WriteProgramArgs():
                act(WriteProgram(me, args.tier))
            case MakeMaterialArgs():
                act(MakeMaterial(me))
            case BuyParcelArgs():
                act(BuyParcel(me))
                parcel = self.game.world.parcels[-1]
                return f"bought parcel {parcel.id}"
            case BuildAdArgs():
                act(BuildAd(me))
                world = self.game.world
                square = plot_at(world, _resident(world, me).position)
                income = ad_income(world, square.col, square.row)
                return f"built an ad slot on {square.id}; it would earn {income} coins a cycle now"
            case BuildServerArgs():
                act(BuildServer(me, args.fee))
                square = plot_at(self.game.world, _resident(self.game.world, me).position)
                return (
                    f"built a server on {square.id} charging {args.fee} coins a cycle; it holds "
                    "no compute yet, supply it with supply_server"
                )
            case BuildMeetingRoomArgs():
                act(BuildMeetingRoom(me))
                world = self.game.world
                square = plot_at(world, _resident(world, me).position)
                income = meeting_income(world, square.col, square.row)
                host = host_for(world, square.centre, me)
                if host is None:
                    return (
                        f"built a meeting room on {square.id}; it would earn {income} coins a "
                        "cycle now, but no server in reach holds compute to host it"
                    )
                return (
                    f"built a meeting room on {square.id}; it would earn {income} coins a cycle "
                    f"now, hosted by server {host.id} for {hosting_fee(host, me)}"
                )
            case SupplyServerArgs():
                act(SupplyServer(me, args.amount))
                world = self.game.world
                square = plot_at(world, _resident(world, me).position)
                server = next(p for p in world.parcels if p.id == square.id).facility
                assert server is not None
                return f"server {square.id} now holds {server.compute} compute"
            case SetHostingFeeArgs():
                act(SetHostingFee(me, args.server, args.fee))
                return f"server {args.server} now charges {args.fee} coins a cycle"
            case SellComputeArgs():
                held = (
                    self.seen_pressure
                    if self.game.world.time - self.seen_at <= PRICE_HOLD_SECONDS
                    else None
                )
                act(SellCompute(me, args.quantity, held))
                market = self.game.world.market
                if held is not None and market is not None:
                    # The resident's own sale weighs on their next one, as it does on others'.
                    self.seen_pressure = min(
                        pressure_at(market, self.game.world.time),
                        held + args.quantity,
                    )
            case BuyProgramArgs():
                act(BuyProgram(me, args.tier))
            case SayArgs():
                act(Say(me, args.text, args.to))
            case OfferToBuyArgs():
                act(OfferToBuy(me, args.seller_id, args.kind, args.quantity, args.price))
            case OfferToSellArgs():
                act(OfferToSell(me, args.buyer_id, args.kind, args.quantity, args.price))
            case OfferInformationArgs():
                act(OfferInformation(me, args.buyer_id, args.summary, args.content, args.price))
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

    def _require_known_spring(self, spring_id: str) -> None:
        """Refuse a spring the resident has never seen as if it did not exist, so that asking
        for ids does not turn up springs they have not found."""
        world = self.game.world
        me = next(resident for resident in world.residents if resident.id == self.resident_id)
        known = {sighting.spring_id for sighting in me.memory.known_springs}
        if spring_id not in known | springs_in_sight(world, self.resident_id):
            raise ActionRejected("unknown_spring", "unknown spring")

    def _require_known_collector(self, collector_id: int) -> None:
        """Refuse a collector the resident cannot know of, see `observation.known_slots`, as if it
        did not exist, so that asking for ids does not turn up collectors or the springs they stand
        on."""
        world = self.game.world
        in_sight = springs_in_sight(world, self.resident_id)
        collector = next(
            (collector for collector in world.collectors if collector.id == collector_id), None
        )
        if collector is None or known_slots(collector, self.resident_id, in_sight) is None:
            raise ActionRejected("unknown_collector", "unknown collector")

    def _act(self, action: Action) -> None:
        """Carry out a primitive as `Game.act` does, unless it would spend the resident's last
        compute. Raises `ActionRejected` or `_LastCompute`, leaving the world as it was."""
        self.game.tick()
        before = self.game.world
        after = perform(before, action)
        me = self.resident_id
        if _compute_in_hand(before, me) > 0 and _compute_in_hand(after, me) == 0:
            raise _LastCompute
        self.game.commit(after)

    def status(self) -> dict[str, Any]:
        """A short summary of the resident right now, handed back after every call."""
        world = self.game.world
        me = next(resident for resident in world.residents if resident.id == self.resident_id)
        items = me.inventory.counts()
        compute = items.pop(COMPUTE, 0)
        shown: dict[str, Any] = {"coins": me.coins, "compute": compute, "items": items}
        if me.walking is not None:
            target = me.walking.target
            shown["walking_to"] = {"x": round(target.x, 1), "y": round(target.y, 1)}
            shown["arrives_in"] = round(max(me.walking.arrives_at - world.time, 0.0), 1)
        if me.harvesting is not None:
            shown["harvesting_spring"] = me.harvesting.spring_id
            shown["harvesting_done_in"] = round(max(me.harvesting.ends_at - world.time, 0.0), 1)
        if me.gathering is not None:
            shown["gathering_mine"] = me.gathering.mine_id
            shown["gathering_done_in"] = round(max(me.gathering.ends_at - world.time, 0.0), 1)
        if me.writing is not None:
            shown["writing_program"] = me.writing.tier
            shown["writing_done_in"] = round(max(me.writing.ends_at - world.time, 0.0), 1)
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


def _compute_in_hand(world: World, resident_id: str) -> int:
    """Compute the resident carries or is drawing, so can still spend; compute set aside in
    a sale offer is not, since the buyer may take it."""
    resident = next(resident for resident in world.residents if resident.id == resident_id)
    harvested = resident.harvesting.amount if resident.harvesting is not None else 0
    return item_count(world, resident_id, COMPUTE) + harvested
