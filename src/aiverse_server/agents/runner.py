"""The loop that drives one resident: look, let the brain act, sleep until worth waking.

Each round the runner calls `Game.begin_turn`, turns the perception into an observation and
hands it, with tools bound to the resident, to the brain. Then it sleeps until one of these
happens, see decision 0003 in the client repository:

- the resident stops being busy: a walk has ended;
- while free, an offer arrives for them, a contract of theirs ends other than by their
  own doing, such as the supplier's last delivery or the contract lapsing, or someone else's
  order fills one of theirs resting on the exchange;
- someone says something they hear, even while busy, since they can still answer;
- while free, `Pace.idle` passes with nothing of the above, so a brain that chose to wait
  is asked again.

Rounds start at least `Pace.interval` apart, `Pace.retry` after a brain failed, which bounds
how often a model is asked and blunts the edge of a fast one. The loop stops when the
resident leaves or the game ends, or when its task is cancelled because the agent was
stopped; a brain holding connections is then closed. A brain that raises is recorded and,
once the resident is free, asked again after `Pace.retry`; it never stops the game or other
agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from ..game.lifecycle import GameError
from .llm_brain import ChatCompletionsBrain
from .observation import Observation, observe, to_json
from .tools import ResidentTools, ResourceBalance, ToolCall, resource_balance

if TYPE_CHECKING:
    from ..server.app import Game

logger = logging.getLogger(__name__)


class Brain(Protocol):
    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        """Decide and act for one round through `tools`; returns a remark for the record,
        such as what a model said it was thinking, or None."""
        ...


@runtime_checkable
class Closable(Protocol):
    """A brain holding something to let go of once it stops, such as an HTTP client."""

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Pace:
    """Real seconds; the game clock runs at the same rate."""

    interval: float = 3.0
    """Least time from the start of one round to the start of the next."""
    retry: float = 10.0
    """Least time to the next round after the brain failed."""
    idle: float = 20.0
    """A free resident with nothing new is asked again after this long."""


DEFAULT_PACE = Pace()


@dataclass(frozen=True)
class TurnRecord:
    """What happened in one round, for logs and later for the page."""

    resident_id: str
    time: float
    """Game seconds when the round began."""
    observation: str
    """The observation as JSON."""
    calls: tuple[ToolCall, ...]
    remark: str | None = None
    error: str | None = None
    before: ResourceBalance | None = None
    after: ResourceBalance | None = None
    ended_at: float = 0.0
    started_at: str | None = None
    finished_at: str | None = None
    memory_after: dict[str, Any] | None = None
    outputs: tuple[str, ...] = ()
    completions: tuple[dict[str, Any], ...] = ()
    """Per model request: why it stopped and the API tokens it used, see `ChatCompletionsBrain`."""
    model: str | None = None
    strategy: str | None = None
    cancelled: bool = False


def redact_turn(turn: TurnRecord, redact: Callable[[str], str]) -> TurnRecord:
    """Clean textual payloads before JSON escaping, keeping schema keys and routing intact."""

    def cleaned(value: Any, argument_keys: bool = False) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {
                redact(key) if argument_keys else key: cleaned(item, argument_keys)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [cleaned(item, argument_keys) for item in value]
        return value

    return replace(
        turn,
        observation=json.dumps(cleaned(json.loads(turn.observation)), ensure_ascii=False),
        calls=tuple(
            replace(
                call,
                name=redact(call.name),
                arguments=cleaned(dict(call.arguments), True),
                result=replace(
                    call.result,
                    message=redact(call.result.message),
                    status=cleaned(call.result.status),
                ),
            )
            for call in turn.calls
        ),
        remark=redact(turn.remark) if turn.remark is not None else None,
        error=redact(turn.error) if turn.error is not None else None,
        memory_after=cleaned(turn.memory_after),
        completions=tuple(cleaned(turn.completions)),
        outputs=tuple(redact(output) for output in turn.outputs),
        strategy=redact(turn.strategy) if turn.strategy is not None else None,
    )


def record_to_json(turn: TurnRecord) -> str:
    """One line of JSON for a log file."""
    shown = asdict(turn)
    shown["observation"] = json.loads(turn.observation)
    # Arguments are kept as a model sent them, which may hold NaN; standard JSON has none.
    return json.dumps(_finite(shown), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _finite(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(item) for item in value]
    return value


def summarize(turn: TurnRecord) -> str:
    """One short line for the console."""
    calls = ", ".join(
        call.name if call.result.ok else f"{call.name}✗{call.result.code}" for call in turn.calls
    )
    text = f"[{turn.time:7.1f}s] {turn.resident_id}: {calls or '-'}"
    if turn.error is not None:
        text += f" ERROR {turn.error}"
    elif turn.remark:
        text += f" | {turn.remark[:120]}"
    return text


_Outcome = Literal["done", "brain_failed", "round_failed"]
"""How a round went: the brain raised, or something else in the round, such as its record."""


@dataclass(frozen=True)
class _Attention:
    """What the runner compares to decide whether to wake the resident."""

    free: bool
    offers: frozenset[int] = field(default_factory=frozenset)
    ended: frozenset[int] = field(default_factory=frozenset)
    """Ids of the resident's contracts that ended other than by their own doing."""
    heard: int = 0
    """Sequence number of the latest line said by someone else in the resident's hearing."""
    done: int = 0
    """Sequence number of the resident's latest activity ending a walk."""
    announced: int = 0
    """Sequence number of the latest notice the server announced to everyone."""
    filled: int = 0
    """Sequence number of the latest fill of one of the resident's resting orders."""


class AgentRunner:
    def __init__(
        self,
        game: Game,
        resident_id: str,
        brain: Brain,
        pace: Pace = DEFAULT_PACE,
        record: Callable[[TurnRecord], None] | None = None,
    ) -> None:
        self.game = game
        self.resident_id = resident_id
        self.brain = brain
        self.pace = pace
        self._record = record
        self._thinking: list[float] = []
        """Game seconds each finished round took, oldest first, shown to the brain."""
        if isinstance(brain, ChatCompletionsBrain):
            game.register_redactor(*brain.credential_redactor())

    async def run(self) -> None:
        changed = self.game.subscribe()
        try:
            while not self._finished():
                started = asyncio.get_running_loop().time()
                # Offers, speech, ended contracts and finished walks as of the look, so those
                # arriving while the brain thinks still wake it, a walk that also ended
                # meanwhile included; free or busy as the brain left the resident.
                before = self._attention()
                outcome = await self._take_turn()
                seen = replace(before, free=self._attention().free)
                await self._sleep(changed, started, outcome, seen)
        finally:
            self.game.unsubscribe(changed)
            if isinstance(self.brain, Closable):
                await self.brain.aclose()

    async def _take_turn(self) -> _Outcome:
        """One round; returns how it went. Nothing raised here stops the loop: a failure is
        logged and the next round waits at least `Pace.retry`."""
        try:
            perception = self.game.begin_turn(self.resident_id)
        except GameError:
            # Left or over since the last look; the loop notices and stops.
            return "done"
        cancelled = False
        started_at = datetime.now(UTC).isoformat()
        try:
            observation = observe(
                self.game.world,
                self.resident_id,
                perception,
                self.game.remaining(),
                self._thinking,
            )
            tools = ResidentTools(self.game, self.resident_id)
            balance = resource_balance(self.game, self.resident_id)
            remark: str | None = None
            error: str | None = None
            try:
                remark = await self.brain.take_turn(observation, tools)
            except asyncio.CancelledError:
                cancelled = True
                error = "agent stopped before completing the round"
            except Exception as failure:
                detail = f"{type(failure).__name__}: {failure}"
                detail = self.game.redact(detail)
                logger.error("agent for %s failed: %s", self.resident_id, detail[:500])
                error = detail[:500]
            # The model may have waited without calling a tool or another world event.
            self.game.tick()
            if not cancelled:
                self._thinking.append(self.game.world.time - observation.time)
            turn = TurnRecord(
                self.resident_id,
                observation.time,
                to_json(observation),
                tuple(tools.calls),
                remark,
                error,
                balance,
                resource_balance(self.game, self.resident_id),
                self.game.world.time,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                memory_after=asdict(
                    next(
                        person.memory
                        for person in self.game.world.residents
                        if person.id == self.resident_id
                    )
                ),
                outputs=tuple(self.brain.outputs)
                if isinstance(self.brain, ChatCompletionsBrain)
                else ((remark,) if remark else ()),
                completions=tuple(self.brain.completions)
                if isinstance(self.brain, ChatCompletionsBrain)
                else (),
                model=self.brain.model if isinstance(self.brain, ChatCompletionsBrain) else None,
                strategy=self.brain.strategy
                if isinstance(self.brain, ChatCompletionsBrain)
                else None,
                cancelled=cancelled,
            )
            turn = redact_turn(turn, self.game.redact)
            if not cancelled:
                self.game.record_turn(turn)
            if self._record is not None:
                self._record(turn)
        except Exception:
            logger.exception("round for %s failed", self.resident_id)
            return "round_failed"
        finally:
            if cancelled:
                raise asyncio.CancelledError
        return "done" if error is None else "brain_failed"

    async def _sleep(
        self, changed: asyncio.Event, started: float, outcome: _Outcome, seen: _Attention
    ) -> None:
        loop = asyncio.get_running_loop()
        failed = outcome != "done"
        earliest = started + (self.pace.retry if failed else self.pace.interval)
        idle_until = max(loop.time() + self.pace.idle, earliest)
        if failed:
            # Awaited even when no time is left, so a brain failing at once cannot hold the
            # event loop while it is asked again and again.
            await asyncio.sleep(max(earliest - loop.time(), 0.0))
        while True:
            # Cleared before looking, so a change made while waiting is never missed.
            changed.clear()
            if self._finished():
                return
            now = loop.time()
            current = self._attention()
            if now >= earliest:
                if current.heard > seen.heard or current.announced > seen.announced:
                    return
                if current.free and (
                    # Only the brain failing: a round whose record failed got its answer.
                    outcome == "brain_failed"
                    or not seen.free
                    or current.done > seen.done
                    or current.offers - seen.offers
                    or current.ended - seen.ended
                    or current.filled > seen.filled
                    or now >= idle_until
                ):
                    return
                timeout: float | None = idle_until - now if current.free else None
            else:
                timeout = earliest - now
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout)

    def _finished(self) -> bool:
        world = self.game.world
        me = next(resident for resident in world.residents if resident.id == self.resident_id)
        return world.ended or me.left

    def _attention(self) -> _Attention:
        world = self.game.world
        me = next(resident for resident in world.residents if resident.id == self.resident_id)
        return _Attention(
            free=not me.busy,
            offers=frozenset(
                event.id for event in world.events.pending if event.recipient_id == me.id
            ),
            ended=frozenset(
                end.contract.id
                for end in world.contracts.ended
                if me.id in (end.contract.supplier_id, end.contract.buyer_id) and end.by != me.id
            ),
            heard=max(
                (line.seq for line in world.speech.entries if me.id in line.listeners),
                default=0,
            ),
            announced=world.notices.next_seq - 1,
            filled=max(
                (
                    fill.seq
                    for fill in world.orders.fills
                    if me.id in (fill.buyer_id, fill.seller_id) and fill.taker_id != me.id
                ),
                default=0,
            ),
            done=max(
                (
                    entry.seq
                    for entry in world.activity.entries
                    if entry.resident_id == me.id and entry.kind == "moved"
                ),
                default=0,
            ),
        )
