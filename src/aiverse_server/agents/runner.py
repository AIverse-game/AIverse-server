"""The loop that drives one resident: look, let the brain act, sleep until worth waking.

Each round the runner calls `Game.begin_turn`, turns the perception into an observation and
hands it, with tools bound to the resident, to the brain. Then it sleeps until one of these
happens, see decision 0003 in the client repository:

- the resident stops being busy: a walk or a mining job has ended;
- while free, an offer arrives for them;
- someone says something they hear, even while busy, since they can still answer;
- while free, `Pace.idle` passes with nothing of the above, so a brain that chose to wait
  is asked again.

Rounds start at least `Pace.interval` apart, `Pace.retry` after a brain failed, which bounds
how often a model is asked and blunts the edge of a fast one. The loop stops when the
resident leaves or the game ends. A brain that raises is recorded and asked again later; it
never stops the game or other agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from ..lifecycle import GameError
from .observation import Observation, observe, to_json
from .tools import ResidentTools, ToolCall

if TYPE_CHECKING:
    from ..app import Game

logger = logging.getLogger(__name__)


class Brain(Protocol):
    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        """Decide and act for one round through `tools`; returns a remark for the record,
        such as what a model said it was thinking, or None."""
        ...


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


@dataclass(frozen=True)
class _Attention:
    """What the runner compares to decide whether to wake the resident."""

    free: bool
    offers: frozenset[int] = field(default_factory=frozenset)
    heard: int = 0
    """Sequence number of the latest line said by someone else in the resident's hearing."""
    done: int = 0
    """Sequence number of the resident's latest activity ending a walk or a mining job."""


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

    async def run(self) -> None:
        changed = self.game.subscribe()
        try:
            while not self._finished():
                started = asyncio.get_running_loop().time()
                # Offers, speech and finished jobs as of the look, so those arriving while the
                # brain thinks still wake it, a walk that also ended meanwhile included; free
                # or busy as the brain left the resident.
                before = self._attention()
                failed = await self._take_turn()
                seen = replace(before, free=self._attention().free)
                await self._sleep(changed, started, failed, seen)
        finally:
            self.game.unsubscribe(changed)

    async def _take_turn(self) -> bool:
        """One round; returns whether it failed. Nothing raised here stops the loop: a failure
        is logged and the resident asked again after `Pace.retry`."""
        try:
            perception = self.game.begin_turn(self.resident_id)
        except GameError:
            # Left or over since the last look; the loop notices and stops.
            return False
        try:
            observation = observe(
                self.game.world, self.resident_id, perception, self.game.remaining()
            )
            tools = ResidentTools(self.game, self.resident_id)
            remark: str | None = None
            error: str | None = None
            try:
                remark = await self.brain.take_turn(observation, tools)
            except Exception as failure:
                logger.exception("agent for %s failed", self.resident_id)
                error = f"{type(failure).__name__}: {failure}"[:500]
            turn = TurnRecord(
                self.resident_id,
                observation.time,
                to_json(observation),
                tuple(tools.calls),
                remark,
                error,
            )
            if self._record is not None:
                self._record(turn)
        except Exception:
            logger.exception("round for %s failed", self.resident_id)
            return True
        return error is not None

    async def _sleep(
        self, changed: asyncio.Event, started: float, failed: bool, seen: _Attention
    ) -> None:
        loop = asyncio.get_running_loop()
        earliest = started + (self.pace.retry if failed else self.pace.interval)
        idle_until = max(loop.time() + self.pace.idle, earliest)
        while True:
            # Cleared before looking, so a change made while waiting is never missed.
            changed.clear()
            if self._finished():
                return
            now = loop.time()
            current = self._attention()
            if now >= earliest:
                if current.heard > seen.heard:
                    return
                if current.free and (
                    not seen.free
                    or current.done > seen.done
                    or current.offers - seen.offers
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
            free=me.walking is None and me.mining is None,
            offers=frozenset(
                event.id for event in world.events.pending if event.recipient_id == me.id
            ),
            heard=max(
                (line.seq for line in world.speech.entries if me.id in line.listeners),
                default=0,
            ),
            done=max(
                (
                    entry.seq
                    for entry in world.activity.entries
                    if entry.resident_id == me.id and entry.kind in ("moved", "finished_mining")
                ),
                default=0,
            ),
        )
