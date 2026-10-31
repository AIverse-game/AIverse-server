"""Tools an agent acts through, bound to the one resident it drives.

None of the tools names who acts: `ResidentTools` holds the resident, so an agent can only
ever move its own, as change 0015 in the client repository asks. Each tool is a primitive
from `actions.py` carried out through `Game.act`, or a note through `Game.remember` and
`Game.forget`, so agents and pages go through the same rules and broadcasts.

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

from ..core.world import Position
from ..economy.contracts import MAX_CONTRACT_SECONDS, MAX_DELIVERIES
from ..economy.tokens import TOKEN, token_total
from ..game.actions import (
    ActionRejected,
    BuildRig,
    BuyChip,
    CancelContract,
    Deliver,
    FundRig,
    InstallChip,
    Mine,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    ProposeContract,
    RemoveChip,
    RespondToEvent,
    Say,
    SellTokens,
    WithdrawEvent,
)
from ..game.lifecycle import GameError, total_coins
from ..residents.memory import NoteError

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


class MineArgs(_Args):
    pool_id: str = Field(description="Id of the pool you stand at, such as p1.")


class SellTokensArgs(_Args):
    quantity: StrictInt = Field(description="Tokens to sell; you must stand at the market.")


class BuyChipArgs(_Args):
    model: str = Field(description="Chip model: MK1, MK2 or MK3.")


class BuildRigArgs(_Args):
    pool_id: str = Field(description="Pool you stand at; build an automatic mining rig here.")


class InstallChipArgs(_Args):
    rig_id: StrictInt
    chip: str = Field(description="Carried chip model to install: MK1, MK2 or MK3.")
    deposit: StrictInt = Field(description="Coins to prepay for rent and electricity; at least 1.")


class FundRigArgs(_Args):
    rig_id: StrictInt
    slot_id: StrictInt
    amount: StrictInt = Field(description="Additional prepaid coins for your slot; at least 1.")


class RemoveChipArgs(_Args):
    rig_id: StrictInt
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
    kind: str = Field(description='Item kind, such as "token" or a chip model like "MK1".')
    quantity: StrictInt = Field(description="How many items.")
    price: StrictInt = Field(description="Coins for the whole lot, set aside until answered.")


class OfferToSellArgs(_Args):
    buyer_id: str = Field(description="Resident id you sell to.")
    kind: str = Field(description='Item kind, such as "token" or a chip model like "MK1".')
    quantity: StrictInt = Field(description="How many items, set aside until answered.")
    price: StrictInt = Field(description="Coins for the whole lot.")


class RespondToOfferArgs(_Args):
    event_id: StrictInt = Field(
        description="event_id of an offer in offers_to_me or a proposal in "
        "contract_proposals_to_me."
    )
    accept: StrictBool = Field(description="true to accept, false to reject.")


class WithdrawOfferArgs(_Args):
    event_id: StrictInt = Field(
        description="event_id of an offer in my_offers or a proposal in my_contract_proposals."
    )


class ProposeContractArgs(_Args):
    partner_id: str = Field(description="Resident id you propose the contract to.")
    my_role: Literal["supplier", "buyer"] = Field(
        description="supplier: you deliver the items and get paid, your deposits set aside "
        "now; buyer: you pay for each delivery, and all your payments are set aside now."
    )
    kind: str = Field(description='Item kind delivered, such as "token" or "MK1".')
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
        description="long_term lasts the whole game; short_term is cleared after 120 seconds."
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
    tokens: int


def resource_balance(game: Game, resident_id: str) -> ResourceBalance:
    """Owned resources, including escrow; reserving coins is not a loss."""
    return ResourceBalance(
        total_coins(game.world, resident_id), token_total(game.world, resident_id)
    )


@dataclass(frozen=True)
class ToolCall:
    """One call an agent made, for the turn's record."""

    name: str
    arguments: Mapping[str, Any]
    result: ToolResult
    time: float = 0.0
    coins_change: int = 0
    tokens_change: int = 0
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
                "parameters": self.args.model_json_schema(),
            },
        }


TOOLS = (
    ToolSpec(
        "build_rig", "Build a rig at your pool for coins; it needs installed chips.", BuildRigArgs
    ),
    ToolSpec(
        "install_chip",
        "Install your chip at a rig you stand at; prepay rent and power, then move freely.",
        InstallChipArgs,
    ),
    ToolSpec(
        "fund_rig",
        "Remotely top up your slot. An unfunded slot pauses without debt or catch-up.",
        FundRigArgs,
    ),
    ToolSpec(
        "remove_chip", "Remotely remove your chip and refund unused prepayment.", RemoveChipArgs
    ),
    ToolSpec(
        "move_to",
        "Walk in a straight line to a point. Costs 1 token per map unit, paid on setting off; "
        "you are busy until you arrive.",
        MoveToArgs,
    ),
    ToolSpec(
        "mine",
        "Mine the pool you stand at with the best chip you carry; the tokens arrive when the "
        "job ends and you are busy until then.",
        MineArgs,
    ),
    ToolSpec(
        "sell_tokens",
        "Sell carried tokens to the market for coins at the market price, which falls the more "
        "is sold. Selling your last token takes you out of the game.",
        SellTokensArgs,
    ),
    ToolSpec("buy_chip", "Buy one chip at the market for its fixed price in coins.", BuyChipArgs),
    ToolSpec(
        "say",
        "Say something aloud to residents nearby, or whisper to one resident anywhere. Costs "
        "tokens.",
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
        "respond_to_offer",
        "Accept or reject an offer or contract proposal sent to you.",
        RespondToOfferArgs,
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
        # Settle timed work before measuring this action, so a completed mine is not
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
                after.tokens - before.tokens,
                timestamp,
            )
        )
        return result

    def move_to(self, x: float, y: float) -> ToolResult:
        return self.call("move_to", {"x": x, "y": y})

    def mine(self, pool_id: str) -> ToolResult:
        return self.call("mine", {"pool_id": pool_id})

    def sell_tokens(self, quantity: int) -> ToolResult:
        return self.call("sell_tokens", {"quantity": quantity})

    def buy_chip(self, model: str) -> ToolResult:
        return self.call("buy_chip", {"model": model})

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
            self._run(args)
        except ActionRejected as rejection:
            return ToolResult(False, rejection.code, rejection.message, self.status())
        except (GameError, NoteError) as error:
            return ToolResult(False, error.code, error.message, self.status())
        return ToolResult(True, status=self.status())

    def _run(self, args: _Args) -> None:
        me = self.resident_id
        act = self.game.act
        match args:
            case BuildRigArgs():
                act(BuildRig(me, args.pool_id))
            case InstallChipArgs():
                act(InstallChip(me, args.rig_id, args.chip, args.deposit))
            case FundRigArgs():
                act(FundRig(me, args.rig_id, args.slot_id, args.amount))
            case RemoveChipArgs():
                act(RemoveChip(me, args.rig_id, args.slot_id))
            case MoveToArgs():
                act(MoveTo(me, Position(args.x, args.y)))
            case MineArgs():
                act(Mine(me, args.pool_id))
            case SellTokensArgs():
                act(SellTokens(me, args.quantity))
            case BuyChipArgs():
                act(BuyChip(me, args.model))
            case SayArgs():
                act(Say(me, args.text, args.to))
            case OfferToBuyArgs():
                act(OfferToBuy(me, args.seller_id, args.kind, args.quantity, args.price))
            case OfferToSellArgs():
                act(OfferToSell(me, args.buyer_id, args.kind, args.quantity, args.price))
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

    def status(self) -> dict[str, Any]:
        """A short summary of the resident right now, handed back after every call."""
        world = self.game.world
        me = next(resident for resident in world.residents if resident.id == self.resident_id)
        items = me.inventory.counts()
        tokens = items.pop(TOKEN, 0)
        shown: dict[str, Any] = {"coins": me.coins, "tokens": tokens, "items": items}
        if me.walking is not None:
            target = me.walking.target
            shown["walking_to"] = {"x": round(target.x, 1), "y": round(target.y, 1)}
            shown["arrives_in"] = round(max(me.walking.arrives_at - world.time, 0.0), 1)
        if me.mining is not None:
            shown["mining_pool"] = me.mining.pool_id
            shown["mining_done_in"] = round(max(me.mining.ends_at - world.time, 0.0), 1)
        if me.left:
            shown["left_game"] = True
        if world.ended:
            shown["game_over"] = True
        return shown
