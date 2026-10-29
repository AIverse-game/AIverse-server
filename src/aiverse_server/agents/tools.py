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
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from ..core.world import Position
from ..economy.tokens import TOKEN
from ..game.actions import (
    ActionRejected,
    BuyChip,
    Mine,
    MoveTo,
    OfferToBuy,
    OfferToSell,
    RespondToEvent,
    Say,
    SellTokens,
    WithdrawEvent,
)
from ..game.lifecycle import GameError
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
    event_id: StrictInt = Field(description="event_id of an offer in offers_to_me.")
    accept: StrictBool = Field(description="true to accept and trade, false to reject.")


class WithdrawOfferArgs(_Args):
    event_id: StrictInt = Field(description="event_id of an offer in my_offers.")


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
class ToolCall:
    """One call an agent made, for the turn's record."""

    name: str
    arguments: Mapping[str, Any]
    result: ToolResult


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
    ToolSpec("respond_to_offer", "Accept or reject an offer sent to you.", RespondToOfferArgs),
    ToolSpec(
        "withdraw_offer", "Take back an offer you sent that is not answered.", WithdrawOfferArgs
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
        if isinstance(arguments, str):
            try:
                loaded = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                # Malformed JSON, or an integer too long to convert.
                loaded = None
            if not isinstance(loaded, dict):
                result = ToolResult(False, "invalid_arguments", "arguments must be a JSON object")
                self.calls.append(ToolCall(name, {"_raw": arguments[:300]}, result))
                return result
            arguments = loaded
        result = self._dispatch(name, arguments)
        self.calls.append(ToolCall(name, dict(arguments), result))
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
