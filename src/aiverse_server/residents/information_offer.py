"""Information offers: a resident sells what they know, and keeps it.

A seller writes an `InformationOffer` for another resident: a short summary, which the
buyer sees while deciding, the full content, which the buyer sees only after paying, and a
price in coins. The content is free text; the server does not check that it is true, so the
buyer relies on the seller's word and on whatever trust they have built.

It is an event, see `events.py`. Nothing is set aside when it is sent, since information is
copied rather than handed over: the seller can offer the same content to several residents
and still has it. When the recipient accepts, their coins go to the seller and the content
is written into the recipient's memory as a long-term note, see `memory.py`, that names the
seller and the summary. If the recipient cannot pay, or their long-term memory is full,
nothing changes and the offer stays pending, so they can still reject it, or `forget` a
note and accept. A seller may have at most `MAX_PENDING_PER_PAIR` offers waiting for the
same recipient.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from dataclasses import dataclass
from typing import Literal

from ..core.events import Event, EventPayload
from ..core.inventory import add_coins, take_coins
from ..core.world import NOTE_LENGTH_LIMIT, World
from ..economy.trade import MAX_AMOUNT
from .memory import remember

SUMMARY_LIMIT = 60
"""Characters a summary may hold: enough to say what the information is about."""

MAX_PENDING_PER_PAIR = 3
"""Offers one resident may have waiting for the same recipient. Information costs the seller
nothing to offer, so without a limit they could bury the recipient in offers."""

InformationErrorCode = Literal["invalid_information", "too_many_offers"]


class InformationError(Exception):
    def __init__(self, code: InformationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: InformationErrorCode = code
        self.message = message


@dataclass(frozen=True)
class InformationOffer(EventPayload):
    """The sender sells a copy of what they know to the recipient; nothing is set aside."""

    summary: str
    """What the buyer sees before paying."""
    content: str
    """What the buyer gets once they have paid."""
    price: int
    """Coins for the information."""

    def note_text(self, seller_id: str) -> str:
        """The note the buyer gets: the content, with who sold it and what it was said to be."""
        return f"Bought from {seller_id} ({self.summary.strip()}): {self.content.strip()}"

    def check(self, seller_id: str) -> None:
        """Refuse an offer that cannot be made. The fields come from untrusted requests, so
        this raises `InformationError`, and its messages leave the values out."""
        for name, text in (("summary", self.summary), ("content", self.content)):
            if type(text) is not str or not text.strip():
                raise InformationError("invalid_information", f"{name} must be non-empty text")
        if len(self.summary.strip()) > SUMMARY_LIMIT:
            raise InformationError(
                "invalid_information", f"summary holds at most {SUMMARY_LIMIT} characters"
            )
        # At least one coin, as in trades: the design allows no free transfers.
        if type(self.price) is not int or not 1 <= self.price <= MAX_AMOUNT:
            raise InformationError(
                "invalid_information", f"price must be an integer in 1..{MAX_AMOUNT} coins"
            )
        # The buyer keeps it as one note, so everything the note says must fit.
        length = len(self.note_text(seller_id))
        if length > NOTE_LENGTH_LIMIT:
            raise InformationError(
                "invalid_information",
                f"summary and content together may take at most {NOTE_LENGTH_LIMIT} characters "
                f"in the buyer's note, which also names you; this one takes {length}",
            )

    def on_send(self, world: World, event: Event) -> World:
        self.check(event.sender_id)
        waiting = sum(
            other.sender_id == event.sender_id
            and other.recipient_id == event.recipient_id
            and isinstance(other.payload, InformationOffer)
            for other in world.events.pending
        )
        if waiting >= MAX_PENDING_PER_PAIR:
            raise InformationError(
                "too_many_offers",
                f"you already have {MAX_PENDING_PER_PAIR} information offers waiting for them",
            )
        return world

    def on_accept(self, world: World, event: Event) -> World:
        world = take_coins(world, event.recipient_id, self.price)
        world = add_coins(world, event.sender_id, self.price)
        return remember(world, event.recipient_id, self.note_text(event.sender_id), "long_term")[0]
