"""Contracts: standing deals that settle future deliveries between two residents.

A contract says "each time the supplier delivers 30 tokens to the buyer, the buyer pays 500
coins, up to 4 times within the next 300 seconds". A single delivery is a contract for one.
Either party proposes it with a `ContractProposal`, an event, see `events.py`, and it comes
into force when the other accepts. The clock starts then.

The buyer's side is guaranteed: on signing, their coins for every delivery are set aside in
the contract, and each delivery pays the supplier from them at once, so a buyer cannot go
back on the deal. A buyer who proposes sets the coins aside when proposing, as a purchase
offer does; a supplier's proposal takes them from the buyer on acceptance, which fails and
leaves the proposal pending when the buyer lacks them. The supplier's side is not
guaranteed: they deliver when they choose and may cancel the rest, and the buyer only gets
their unspent coins back.

A contract ends after its last delivery, when it lapses at `expires_at`, when the supplier
cancels it, when either party leaves the game, or when the game ends; whatever is still set
aside goes back to the buyer. The set-aside coins count for the buyer in the ranking.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from ..core.events import EventPayload
from ..core.inventory import add_coins, store_item, take_coins, take_item
from ..core.world import Contract, ContractBook
from .trade import MAX_AMOUNT

if TYPE_CHECKING:
    from ..core.events import Event
    from ..core.world import World

MAX_DELIVERIES = 100
"""Deliveries one contract may promise."""

MAX_CONTRACT_SECONDS = 3600
"""Longest a contract may run; the game ending stops every contract anyway."""

ContractErrorCode = Literal["invalid_contract", "unknown_contract", "not_supplier"]


class ContractError(Exception):
    def __init__(self, code: ContractErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ContractErrorCode = code
        self.message = message


@dataclass(frozen=True)
class ContractProposal(EventPayload):
    """Terms the sender offers the recipient; the sender supplies if `sender_supplies`,
    otherwise the sender buys and their coins wait set aside."""

    sender_supplies: bool
    kind: str
    """The item kind delivered."""
    quantity: int
    """Items in one delivery."""
    price: int
    """Coins for one delivery."""
    deliveries: int
    duration: int
    """Seconds from signing until the contract lapses."""

    @property
    def total_price(self) -> int:
        return self.price * self.deliveries

    def check(self) -> None:
        """Refuse terms that cannot make a contract. They come from untrusted requests, so
        this raises `ContractError` rather than `ValueError`; messages leave values out."""
        if type(self.sender_supplies) is not bool:
            raise ContractError("invalid_contract", "who supplies must be true or false")
        if type(self.kind) is not str or not self.kind:
            raise ContractError("invalid_contract", "item kind must be a non-empty string")
        bounds = (
            ("quantity", self.quantity, MAX_AMOUNT),
            ("price", self.price, MAX_AMOUNT),
            ("deliveries", self.deliveries, MAX_DELIVERIES),
            ("duration", self.duration, MAX_CONTRACT_SECONDS),
        )
        for name, value, most in bounds:
            # Exactly int, as for trades: True is not one delivery.
            if type(value) is not int or not 1 <= value <= most:
                raise ContractError("invalid_contract", f"{name} must be an integer in 1..{most}")
        if self.total_price > MAX_AMOUNT:
            raise ContractError(
                "invalid_contract", f"price times deliveries must be at most {MAX_AMOUNT} coins"
            )

    def parties(self, event: Event) -> tuple[str, str]:
        """Supplier and buyer."""
        if self.sender_supplies:
            return event.sender_id, event.recipient_id
        return event.recipient_id, event.sender_id

    def on_send(self, world: World, event: Event) -> World:
        self.check()
        if self.sender_supplies:
            return world
        return take_coins(world, event.sender_id, self.total_price)

    def on_accept(self, world: World, event: Event) -> World:
        supplier_id, buyer_id = self.parties(event)
        if self.sender_supplies:
            world = take_coins(world, buyer_id, self.total_price)
        book = world.contracts
        contract = Contract(
            id=book.next_id,
            supplier_id=supplier_id,
            buyer_id=buyer_id,
            kind=self.kind,
            quantity=self.quantity,
            price=self.price,
            deliveries_left=self.deliveries,
            signed_at=world.time,
            expires_at=world.time + self.duration,
        )
        return replace(world, contracts=ContractBook((*book.active, contract), book.next_id + 1))

    def on_decline(self, world: World, event: Event) -> World:
        if self.sender_supplies:
            return world
        return add_coins(world, event.sender_id, self.total_price)


def contract_coins(world: World, resident_id: str) -> int:
    """Coins the resident has set aside as a buyer: in contracts in force and in proposals
    they sent that are still waiting for an answer."""
    proposed = sum(
        event.payload.total_price
        for event in world.events.pending
        if event.sender_id == resident_id
        and isinstance(event.payload, ContractProposal)
        and not event.payload.sender_supplies
    )
    held = sum(
        contract.escrow for contract in world.contracts.active if contract.buyer_id == resident_id
    )
    return proposed + held


def contracts_of(world: World, resident_id: str) -> tuple[Contract, ...]:
    """Contracts in force the resident is a party to, oldest first."""
    return tuple(
        contract
        for contract in world.contracts.active
        if resident_id in (contract.supplier_id, contract.buyer_id)
    )


def deliver(world: World, resident_id: str, contract_id: int) -> World:
    """The supplier hands one delivery to the buyer and is paid for it from the coins set
    aside; the last delivery ends the contract. Refused, changing nothing, when the
    resident is not the supplier or lacks the items."""
    contract = _find(world, resident_id, contract_id)
    if contract.supplier_id != resident_id:
        raise ContractError(
            "not_supplier", f"only the supplier delivers under contract {contract_id}"
        )
    world = take_item(world, resident_id, contract.kind, contract.quantity)
    world = store_item(world, contract.buyer_id, contract.kind, contract.quantity)
    world = add_coins(world, resident_id, contract.price)
    rest = replace(contract, deliveries_left=contract.deliveries_left - 1)
    return _replace(world, contract, rest if rest.deliveries_left > 0 else None)


def cancel_contract(world: World, resident_id: str, contract_id: int) -> World:
    """The supplier calls off the deliveries still to come; the buyer gets their coins back.
    The buyer cannot cancel: the supplier may already have worked towards the deal."""
    contract = _find(world, resident_id, contract_id)
    if contract.supplier_id != resident_id:
        raise ContractError("not_supplier", f"only the supplier can cancel contract {contract_id}")
    return _end(world, contract)


def end_contracts(world: World, ends: Callable[[Contract], bool]) -> World:
    """End every contract `ends` picks, handing the buyers back what is set aside."""
    for contract in world.contracts.active:
        if ends(contract):
            world = _end(world, contract)
    return world


def expire_contracts(world: World) -> World:
    """End the contracts that have lapsed by `world.time`; returns `world` itself when none
    has."""
    return end_contracts(world, lambda contract: contract.expires_at <= world.time)


def next_expiry(world: World) -> float | None:
    """Game time when the earliest contract lapses, or None when none is in force."""
    return min((contract.expires_at for contract in world.contracts.active), default=None)


def _find(world: World, resident_id: str, contract_id: int) -> Contract:
    for contract in world.contracts.active:
        # Someone else's contract is reported as missing, so its existence does not leak.
        if contract.id == contract_id and resident_id in (contract.supplier_id, contract.buyer_id):
            if contract.expires_at <= world.time:
                break
            return contract
    raise ContractError(
        "unknown_contract", f"resident {resident_id!r} has no contract {contract_id!r} in force"
    )


def _end(world: World, contract: Contract) -> World:
    return _replace(add_coins(world, contract.buyer_id, contract.escrow), contract, None)


def _replace(world: World, contract: Contract, by: Contract | None) -> World:
    """Put `by` in the contract's place, or drop the contract for None."""
    book = world.contracts
    swapped = (by if other.id == contract.id else other for other in book.active)
    active = tuple(other for other in swapped if other is not None)
    return replace(world, contracts=replace(book, active=active))
