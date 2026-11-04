"""Contracts: standing deals that settle future deliveries between two residents.

A contract says "the supplier delivers 30 energy to the buyer 4 times within the next 300
seconds, and each delivery pays them 500 coins". A single delivery is a contract for one.
Either party proposes it with a `ContractProposal`, an event, see `events.py`, and it comes
into force when the other accepts. The clock starts then.

The buyer's side is guaranteed: on signing, their coins for every delivery are set aside in
the contract, and each delivery pays the supplier from them at once, so a buyer cannot go
back on the deal. The supplier's side is guaranteed as far as their deposit goes: they may
stake some coins on each delivery, set aside alongside the buyer's and handed back with each
delivery. Deliveries not made when the contract lapses or the supplier cancels it, or when
the supplier leaves the game, cost the supplier their deposit, which goes to the buyer. A
deposit of 0, the default, binds the supplier to nothing.

Each party's coins are set aside when they commit: the sender's when proposing, as an offer
does, the recipient's on accepting, which fails and leaves the proposal pending when they
lack them.

A contract ends after its last delivery, when it lapses at `expires_at`, when the supplier
cancels it, when either party leaves the game, or when the game ends; the buyer gets back
their coins still set aside, and the supplier their deposit unless forfeited as above. The
set-aside coins count for each in the ranking. The world keeps the last few endings in
`ContractBook.ended`, so the parties can learn how a contract ended.

Like the rest of the world, every function returns a new world and leaves its argument
untouched; on error nothing changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from ..core.events import EventPayload
from ..core.inventory import add_coins, store_item, take_coins, take_item
from ..core.ledger import record_transfer
from ..core.world import (
    CONTRACT_END_LIMIT,
    Contract,
    ContractEnd,
    ContractEndReason,
)
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
    otherwise the sender buys. The sender's coins, deposit or payment, wait set aside."""

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
    deposit: int = 0
    """Coins the supplier stakes on each delivery."""

    @property
    def total_price(self) -> int:
        return self.price * self.deliveries

    @property
    def total_deposit(self) -> int:
        return self.deposit * self.deliveries

    @property
    def sender_stake(self) -> int:
        """Coins set aside from the sender while the proposal waits."""
        return self.total_deposit if self.sender_supplies else self.total_price

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
        if type(self.deposit) is not int or not 0 <= self.deposit <= MAX_AMOUNT:
            raise ContractError(
                "invalid_contract", f"deposit must be an integer in 0..{MAX_AMOUNT}"
            )
        for name, total in (("price", self.total_price), ("deposit", self.total_deposit)):
            if total > MAX_AMOUNT:
                raise ContractError(
                    "invalid_contract",
                    f"{name} times deliveries must be at most {MAX_AMOUNT} coins",
                )

    def parties(self, event: Event) -> tuple[str, str]:
        """Supplier and buyer."""
        if self.sender_supplies:
            return event.sender_id, event.recipient_id
        return event.recipient_id, event.sender_id

    def on_send(self, world: World, event: Event) -> World:
        self.check()
        return _take(world, event.sender_id, self.sender_stake)

    def on_accept(self, world: World, event: Event) -> World:
        supplier_id, buyer_id = self.parties(event)
        if self.sender_supplies:
            world = take_coins(world, buyer_id, self.total_price)
        else:
            world = _take(world, supplier_id, self.total_deposit)
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
            deposit=self.deposit,
        )
        return replace(
            world,
            contracts=replace(book, active=(*book.active, contract), next_id=book.next_id + 1),
        )

    def on_decline(self, world: World, event: Event) -> World:
        return _give(world, event.sender_id, self.sender_stake)


def contract_coins(world: World, resident_id: str) -> int:
    """Coins the resident has set aside in contracts in force, as the buyer's payments or the
    supplier's deposit, and in proposals they sent that are still waiting for an answer."""
    proposed = sum(
        event.payload.sender_stake
        for event in world.events.pending
        if event.sender_id == resident_id and isinstance(event.payload, ContractProposal)
    )
    held = sum(
        (contract.escrow if contract.buyer_id == resident_id else 0)
        + (contract.deposit_held if contract.supplier_id == resident_id else 0)
        for contract in world.contracts.active
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
    aside, getting that delivery's deposit back too; the last delivery ends the contract.
    Refused, changing nothing, when the resident is not the supplier or lacks the items."""
    contract = _find(world, resident_id, contract_id)
    if contract.supplier_id != resident_id:
        raise ContractError(
            "not_supplier", f"only the supplier delivers under contract {contract_id}"
        )
    world = take_item(world, resident_id, contract.kind, contract.quantity)
    world = store_item(world, contract.buyer_id, contract.kind, contract.quantity)
    world = add_coins(world, resident_id, contract.price)
    world = record_transfer(
        world,
        contract.buyer_id,
        resident_id,
        contract.price,
        "contract_payment",
        contract.kind,
        contract.quantity,
    )
    world = _give(world, resident_id, contract.deposit)
    rest = replace(contract, deliveries_left=contract.deliveries_left - 1)
    if rest.deliveries_left > 0:
        return _replace(world, contract, rest)
    return _record(_replace(world, contract, None), rest, "completed", resident_id, 0)


def cancel_contract(world: World, resident_id: str, contract_id: int) -> World:
    """The supplier calls off the deliveries still to come; the buyer gets their coins back
    and the supplier's deposit for those deliveries. The buyer cannot cancel: the supplier
    may already have worked towards the deal."""
    contract = _find(world, resident_id, contract_id)
    if contract.supplier_id != resident_id:
        raise ContractError("not_supplier", f"only the supplier can cancel contract {contract_id}")
    return _end(world, contract, "cancelled", resident_id)


def end_contracts(
    world: World,
    ends: Callable[[Contract], bool],
    reason: ContractEndReason,
    by: str | None = None,
) -> World:
    """End every contract `ends` picks for `reason`, by the doing of `by` if anyone's,
    handing back or forfeiting what is set aside as the module docstring says."""
    for contract in world.contracts.active:
        if ends(contract):
            world = _end(world, contract, reason, by)
    return world


def expire_contracts(world: World) -> World:
    """End the contracts that have lapsed by `world.time`; returns `world` itself when none
    has."""
    return end_contracts(world, lambda contract: contract.expires_at <= world.time, "lapsed")


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


def _end(world: World, contract: Contract, reason: ContractEndReason, by: str | None) -> World:
    breached = reason in ("lapsed", "cancelled") or (
        reason == "party_left" and by == contract.supplier_id
    )
    forfeit = contract.deposit_held if breached else 0
    world = add_coins(world, contract.buyer_id, contract.escrow)
    world = _give(world, contract.buyer_id, forfeit)
    if forfeit:
        world = record_transfer(
            world, contract.supplier_id, contract.buyer_id, forfeit, "contract_forfeit"
        )
    world = _give(world, contract.supplier_id, contract.deposit_held - forfeit)
    return _record(_replace(world, contract, None), contract, reason, by, forfeit)


def _record(
    world: World,
    contract: Contract,
    reason: ContractEndReason,
    by: str | None,
    forfeit: int,
) -> World:
    book = world.contracts
    end = ContractEnd(contract, world.time, reason, by, forfeit)
    return replace(world, contracts=replace(book, ended=(*book.ended, end)[-CONTRACT_END_LIMIT:]))


def _take(world: World, resident_id: str, coins: int) -> World:
    """`take_coins`, which refuses 0, for amounts that may be 0, such as no deposit."""
    return take_coins(world, resident_id, coins) if coins else world


def _give(world: World, resident_id: str, coins: int) -> World:
    return add_coins(world, resident_id, coins) if coins else world


def _replace(world: World, contract: Contract, by: Contract | None) -> World:
    """Put `by` in the contract's place, or drop the contract for None."""
    book = world.contracts
    swapped = (by if other.id == contract.id else other for other in book.active)
    active = tuple(other for other in swapped if other is not None)
    return replace(world, contracts=replace(book, active=active))
