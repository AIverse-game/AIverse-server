"""HTTP/WebSocket entry point.

Every connection greets with `hello` and receives the map. After that it may send operation
primitives; when one changes the world, every greeted connection receives the new `state`.
A connection may choose a resident to view as with `view_as`; its states then carry that
resident's whispers, offers and contracts, and no one else's. It may also hand a resident to a
language model with `start_agent`, or take them back with `stop_agent`; the server's config
file may override the model settings pages send, see `config.py`.
The game ends a fixed time after the app is created; every connection then receives the
final `state`. Mining jobs and walks finish on the same clock, and are broadcast too.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..agents.llm_brain import (
    NO_SETTINGS,
    ChatCompletionsBrain,
    LLMSettings,
    SettingsError,
    complete,
)
from ..agents.runner import DEFAULT_PACE, AgentRunner, Brain, Pace, TurnRecord
from ..core.world import Note, NoteTerm, World
from ..game.actions import Action, ActionRejected, perform
from ..game.clock import next_due, settle_due
from ..game.layout import create_default_world
from ..game.lifecycle import (
    ENDING_NOTICE_SECONDS,
    GAME_DURATION,
    GameError,
    announce,
    check_can_act,
    end_game,
)
from ..residents.memory import forget, remember
from ..residents.perception import Perception, begin_turn
from .protocol import (
    AGENT_TURN_LIMIT,
    PROTOCOL_VERSION,
    ActionResultMessage,
    AgentRejectionCode,
    AgentResultMessage,
    AgentTurnView,
    ErrorMessage,
    HelloMessage,
    LLMSettingName,
    ServerMessage,
    StartAgentMessage,
    StateMessage,
    StopAgentMessage,
    ViewAsMessage,
    WelcomeMessage,
    action_rejected,
    agent_result,
    agent_turn_view,
    bad_message,
    encode,
    parse_client_message,
    setting_names,
    to_action,
    to_llm_settings,
    world_view,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """What one client frame leads to."""

    replies: list[ServerMessage]
    """Sent back to the sender only."""
    world: World
    """The world afterwards; a different object only when an action changed it."""
    greeted: bool
    """Whether the sender has completed the handshake and may act."""
    viewer: str | None
    """The resident the sender views as, whose whispers and deals its states carry; None
    for nobody.
    No default: every outcome must carry it on, or the connection's choice would be lost."""
    request: StartAgentMessage | StopAgentMessage | None = None
    """A request about agents, for the game to carry out and answer; see `answer_request`."""


def handle_text(
    text: str,
    world: World,
    greeted: bool,
    remaining_seconds: float,
    viewer: str | None = None,
    agents: Mapping[str, str | None] | None = None,
    llm_overrides: Sequence[LLMSettingName] = (),
    agent_turns: Mapping[str, Sequence[AgentTurnView]] | None = None,
) -> Outcome:
    """Handles one client frame. Errors leave the connection open and the world unchanged.
    `remaining_seconds` is the game time left, `viewer` the resident the sender views as and
    `agents` the residents agents drive, for the states that answer `hello` and `view_as`;
    `llm_overrides` the model settings the server fixes, for the welcome."""
    message = parse_client_message(text)
    if isinstance(message, ErrorMessage):
        return Outcome([message], world, greeted, viewer)
    if isinstance(message, HelloMessage):
        if message.protocol_version != PROTOCOL_VERSION:
            mismatch = ErrorMessage(
                code="protocol_mismatch", message=f"server speaks protocol {PROTOCOL_VERSION}"
            )
            return Outcome([mismatch], world, greeted, viewer)
        state = StateMessage(
            world=world_view(world, remaining_seconds, viewer, agents, agent_turns)
        )
        welcome = WelcomeMessage(llm_overrides=list(llm_overrides))
        return Outcome([welcome, state], world, True, viewer)
    if not greeted:
        return Outcome([bad_message("send hello before anything else")], world, greeted, viewer)
    if isinstance(message, ViewAsMessage):
        chosen = message.resident_id
        if chosen is not None and all(resident.id != chosen for resident in world.residents):
            return Outcome([bad_message(f"no resident with id {chosen!r}")], world, greeted, viewer)
        state = StateMessage(
            world=world_view(world, remaining_seconds, chosen, agents, agent_turns)
        )
        return Outcome([state], world, greeted, chosen)
    if isinstance(message, StartAgentMessage | StopAgentMessage):
        return Outcome([], world, greeted, viewer, message)
    try:
        after = perform(world, to_action(message))
    except ActionRejected as rejection:
        return Outcome([action_rejected(rejection)], world, greeted, viewer)
    return Outcome([ActionResultMessage(ok=True)], after, greeted, viewer)


# A connection is closed once this many replies are waiting for it: it sends faster than it
# reads, and keeping more would let one client grow the server's memory without bound.
OUTBOX_LIMIT = 256

# WebSocket close code 1008, policy violation.
CLOSE_POLICY_VIOLATION = 1008

# Closing waits for the client to read what is already buffered, which a client that has
# stopped reading never does; give up after this many seconds and let the server drop it.
CLOSE_TIMEOUT = 5.0


class Outbox:
    """Messages waiting to go out on one connection, sent by its own task.

    Replies to the connection's own frames queue up in order, up to `limit`. A world
    snapshot is complete, so only the latest one waits, and it goes out after the queued
    replies. Moves by other connections, however many, therefore never grow this outbox.
    `viewer` is the resident the connection views as, which decides the whispers, offers
    and contracts in its snapshots.
    """

    def __init__(self, limit: int = OUTBOX_LIMIT) -> None:
        self.viewer: str | None = None
        self.replies: deque[str] = deque()
        self.state: str | None = None
        self.limit = limit
        self.overflowed = asyncio.Event()
        self._ready = asyncio.Event()

    def put(self, text: str) -> None:
        if len(self.replies) >= self.limit:
            self.overflowed.set()
            return
        self.replies.append(text)
        self._ready.set()

    def put_state(self, text: str) -> None:
        self.state = text
        self._ready.set()

    async def get(self) -> str:
        while True:
            if self.replies:
                return self.replies.popleft()
            if self.state is not None:
                text, self.state = self.state, None
                return text
            self._ready.clear()
            await self._ready.wait()


class AgentRejected(Exception):
    def __init__(self, code: AgentRejectionCode, message: str) -> None:
        super().__init__(message)
        self.code: AgentRejectionCode = code
        self.message = message


LLMBrainFactory = Callable[[str, str, str, str | None], Brain]
"""Makes the brain for a base URL, API key and model."""


@dataclass(frozen=True)
class _Agent:
    brain: Brain
    model: str | None
    """The language model the brain asks; None for a scripted one."""
    task: asyncio.Task[None]


class Game:
    """The world all connections share, its clock, the outboxes of those that said hello, and
    the agents driving residents.

    A broadcast reaches every outbox at once, so no connection can see an older state
    after a newer one.
    """

    def __init__(
        self,
        world: World,
        duration: float = GAME_DURATION,
        clock: Callable[[], float] = time.monotonic,
        pace: Pace = DEFAULT_PACE,
        record: Callable[[TurnRecord], None] | None = None,
        llm_override: LLMSettings = NO_SETTINGS,
        llm_brain: LLMBrainFactory = ChatCompletionsBrain,
    ) -> None:
        self.world = world
        self.llm_override = llm_override
        """Model settings from the config file, laid over whatever a page or the command line
        chose."""
        self._clock = clock
        self._start = clock()
        self._duration = duration
        # A game no longer than the warning has no time to give it.
        self._notice_pending = duration > ENDING_NOTICE_SECONDS
        self._pace = pace
        self._record = record
        self._llm_brain = llm_brain
        self._outboxes: set[Outbox] = set()
        # Set on every broadcast: a new job or walk may end before what the timer waits for.
        self._changed = asyncio.Event()
        # One per agent waiting for something to happen, see `agents/runner.py`.
        self._subscribers: set[asyncio.Event] = set()
        self._agents: dict[str, _Agent] = {}
        # Model requests each resident's brains have made this game, so the limit holds per
        # resident however often their agent is restarted.
        self._llm_requests: dict[str, int] = {}
        self.agent_turns: dict[str, deque[AgentTurnView]] = {}
        self._turn_seq = 0
        self._credential_redactors: list[tuple[int, Callable[[str], str]]] = []

    def remaining(self) -> float:
        return max(self._duration - (self._clock() - self._start), 0.0)

    def tick(self) -> None:
        """Bring the world up to now: move its time on, finish the mining jobs and walks that
        are done (see `clock.py`), end the game if its time is up, and tell everyone if
        anything changed. Called by the timer and before each frame, so no action slips in
        late while the timer waits for its turn."""
        if self.world.ended:
            return
        # One reading decides both, so the deadline cannot pass between paying and ending.
        elapsed = self._clock() - self._start
        # Capped at the end, so jobs and walks due after the deadline never finish.
        timed = replace(self.world, time=max(min(elapsed, self._duration), self.world.time))
        # Settled before ending, which only stops jobs and walks: nothing after the end is news.
        after = settle_due(timed)
        if elapsed >= self._duration:
            after = end_game(after)
        elif self._notice_pending and elapsed >= self._duration - ENDING_NOTICE_SECONDS:
            self._notice_pending = False
            after = announce(after, f"The game ends in {ENDING_NOTICE_SECONDS:g} seconds.")
        self.world = after
        if after is not timed:
            self.broadcast_world()

    def act(self, action: Action) -> None:
        """Carry out a primitive for an agent: bring the world up to now, perform it, keep the
        result and tell everyone. Raises `ActionRejected`, leaving the world as it was."""
        self.tick()
        self.commit(perform(self.world, action))

    def commit(self, world: World) -> None:
        """Keep `world` as the shared one and, if it differs, send it to everyone. The one
        place where agents and connections alike change the world."""
        if world is self.world:
            return
        self.world = world
        self.broadcast_world()

    def subscribe(self) -> asyncio.Event:
        """An event set on every broadcast, for an agent waiting for the world to change;
        clear it before looking at the world, then wait on it."""
        changed = asyncio.Event()
        self._subscribers.add(changed)
        return changed

    def unsubscribe(self, changed: asyncio.Event) -> None:
        self._subscribers.discard(changed)

    def begin_turn(self, resident_id: str) -> Perception:
        """Start a round of work for the resident: bring the world up to now, then look
        around and keep what they noticed, see `perception.py`. Raises `GameError` for a
        resident who cannot act. Their memory is not in the protocol, so nothing is sent."""
        self.tick()
        self.world, perception = begin_turn(self.world, resident_id)
        return perception

    def remember(self, resident_id: str, text: str, term: NoteTerm) -> Note:
        """Keep a note in the resident's memory for the whole game or for a short while, as
        they choose, timed from now, see `memory.py`. Raises `GameError` or `NoteError`.
        Like perception, nothing is sent."""
        self.tick()
        self.world, note = remember(self.world, resident_id, text, term)
        return note

    def forget(self, resident_id: str, note_id: int) -> None:
        """Remove one of the resident's notes, see `memory.py`. Raises `GameError` or
        `NoteError`; nothing is sent."""
        self.tick()
        self.world = forget(self.world, resident_id, note_id)

    def register_redactor(self, key_length: int, redact: Callable[[str], str]) -> None:
        """Retain a used credential's filter for this whole game, across agent changes."""
        self._credential_redactors.append((key_length, redact))
        # A short old key may be part of a newer one; mask the complete long value first.
        self._credential_redactors.sort(key=lambda rule: rule[0], reverse=True)

    def redact(self, text: str) -> str:
        for _, redact in self._credential_redactors:
            text = redact(text)
        return text

    def record_turn(self, turn: TurnRecord) -> None:
        """Keep a bounded page projection independently of optional console/file logging."""
        self._turn_seq += 1
        shown = agent_turn_view(turn, self._turn_seq, self.redact)
        self.agent_turns.setdefault(turn.resident_id, deque(maxlen=AGENT_TURN_LIMIT)).append(shown)
        self.broadcast_world()

    def agents(self) -> dict[str, str | None]:
        """The residents agents drive, to the model each asks, None for a scripted brain."""
        return {resident_id: agent.model for resident_id, agent in self._agents.items()}

    def start_agent(self, resident_id: str, brain: Brain, model: str | None = None) -> None:
        """Drive the resident with `brain` from now on, replacing any agent driving them, and
        tell everyone. Needs a running event loop. Raises `AgentRejected` for a resident who
        cannot act."""
        self._check_can_act(resident_id)
        self._cancel(resident_id)
        if isinstance(brain, ChatCompletionsBrain):
            brain.requests = max(brain.requests, self._llm_requests.get(resident_id, 0))
        runner = AgentRunner(self, resident_id, brain, self._pace, self._record)
        task = asyncio.get_running_loop().create_task(runner.run())
        agent = _Agent(brain, model, task)
        self._agents[resident_id] = agent
        task.add_done_callback(lambda _: self._agent_ended(resident_id, agent))
        self.broadcast_world()

    def start_llm_agent(self, resident_id: str, chosen: LLMSettings, strategy: str = "") -> None:
        """Drive the resident with a language model: the config file's settings where it gives
        them, `chosen` for the rest. Raises `AgentRejected` for a resident who cannot act, or
        settings missing or malformed after that."""
        self._check_can_act(resident_id)
        try:
            base_url, api_key, model = complete(self.llm_override.over(chosen))
        except SettingsError as error:
            raise AgentRejected(error.code, error.message) from None
        self.start_agent(
            resident_id, self._llm_brain(base_url, api_key, model, strategy or None), model
        )

    def stop_agent(self, resident_id: str) -> None:
        """Stop the agent driving the resident, if any, and tell everyone. Raises
        `AgentRejected` for an unknown resident."""
        if all(resident.id != resident_id for resident in self.world.residents):
            raise AgentRejected("unknown_resident", f"no resident with id {resident_id!r}")
        if self._cancel(resident_id):
            self.broadcast_world()

    async def stop_agents(self) -> None:
        """Stop every agent and wait until they have let go of what they hold."""
        tasks = [agent.task for agent in self._agents.values()]
        for resident_id in list(self._agents):
            self._cancel(resident_id)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _check_can_act(self, resident_id: str) -> None:
        self.tick()
        try:
            check_can_act(self.world, resident_id)
        except GameError as error:
            raise AgentRejected(error.code, error.message) from None

    def _cancel(self, resident_id: str) -> bool:
        agent = self._agents.pop(resident_id, None)
        if agent is None:
            return False
        self._count_requests(resident_id, agent)
        agent.task.cancel()
        return True

    def _count_requests(self, resident_id: str, agent: _Agent) -> None:
        if isinstance(agent.brain, ChatCompletionsBrain):
            self._llm_requests[resident_id] = agent.brain.requests

    def _agent_ended(self, resident_id: str, agent: _Agent) -> None:
        if not agent.task.cancelled() and agent.task.exception() is not None:
            logger.error("agent for %s stopped", resident_id, exc_info=agent.task.exception())
        # Still listed only if its loop stopped by itself: the resident left or the game ended.
        if self._agents.get(resident_id) is agent:
            del self._agents[resident_id]
            self._count_requests(resident_id, agent)
            self.broadcast_world()

    def _until_next(self) -> float:
        """Seconds until the game ends, the ending is announced, or the next mining job or walk
        is due, whichever comes first."""
        wait = self.remaining()
        if self._notice_pending:
            wait = min(wait, self.remaining() - ENDING_NOTICE_SECONDS)
        due = next_due(self.world)
        if due is not None:
            wait = min(wait, due - (self._clock() - self._start))
        return max(wait, 0.0)

    async def run_clock(self) -> None:
        while not self.world.ended:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), self._until_next())
            self._changed.clear()
            self.tick()

    def watch(self, outbox: Outbox) -> None:
        self._outboxes.add(outbox)

    def unwatch(self, outbox: Outbox) -> None:
        self._outboxes.discard(outbox)

    def broadcast_world(self) -> None:
        """Send the world to every greeted connection as its viewer sees it; one encoding
        per viewer, since only the whispers and deals differ."""
        remaining = self.remaining()
        agents = self.agents()
        texts: dict[str | None, str] = {}
        for outbox in self._outboxes:
            if outbox.viewer not in texts:
                view = world_view(self.world, remaining, outbox.viewer, agents, self.agent_turns)
                texts[outbox.viewer] = encode(StateMessage(world=view))
            outbox.put_state(texts[outbox.viewer])
        self._changed.set()
        for changed in self._subscribers:
            changed.set()


def answer_request(game: Game, request: StartAgentMessage | StopAgentMessage) -> AgentResultMessage:
    try:
        if isinstance(request, StartAgentMessage):
            game.start_llm_agent(request.resident_id, to_llm_settings(request), request.strategy)
        else:
            game.stop_agent(request.resident_id)
    except AgentRejected as rejection:
        return agent_result(request.resident_id, rejection.code, rejection.message)
    return agent_result(request.resident_id)


async def _send_all(websocket: WebSocket, outbox: Outbox) -> None:
    try:
        while True:
            await websocket.send_text(await outbox.get())
    except (WebSocketDisconnect, RuntimeError, OSError):
        # The client is gone; the receive loop notices and cleans up.
        return


async def _receive_all(websocket: WebSocket, game: Game, outbox: Outbox) -> None:
    greeted = False
    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                return
            text = frame.get("text")
            if not isinstance(text, str):
                outbox.put(encode(bad_message("send JSON as a text frame")))
                continue
            # No await from here to the broadcast, so each frame sees and commits the
            # latest world without interleaving with other connections.
            game.tick()
            outcome = handle_text(
                text,
                game.world,
                greeted,
                game.remaining(),
                outbox.viewer,
                game.agents(),
                setting_names(game.llm_override),
                game.agent_turns,
            )
            outbox.viewer = outcome.viewer
            if outcome.greeted and not greeted:
                game.watch(outbox)
            greeted = outcome.greeted
            for reply in outcome.replies:
                # The state answering hello also waits in the slot, behind the welcome.
                if isinstance(reply, StateMessage):
                    outbox.put_state(encode(reply))
                else:
                    outbox.put(encode(reply))
            game.commit(outcome.world)
            if outcome.request is not None:
                outbox.put(encode(answer_request(game, outcome.request)))
    except WebSocketDisconnect:
        # The client left mid-receive; nothing else to clean up.
        return


def create_app(
    world: World | None = None,
    outbox_limit: int = OUTBOX_LIMIT,
    duration: float = GAME_DURATION,
    clock: Callable[[], float] = time.monotonic,
    agents: Mapping[str, Brain] | None = None,
    pace: Pace = DEFAULT_PACE,
    record: Callable[[TurnRecord], None] | None = None,
    llm_override: LLMSettings = NO_SETTINGS,
    llm_brain: LLMBrainFactory = ChatCompletionsBrain,
) -> FastAPI:
    """The game starts now, when its world is created, and ends `duration` seconds later.
    `agents` maps resident ids to the brains that drive them from the start, see
    `agents/runner.py`; pages may start and stop more later, with brains from `llm_brain`
    and settings `llm_override` lays over theirs. `record` receives every round agents
    take. Other residents are left to the pages."""
    game = Game(
        world if world is not None else create_default_world(),
        duration,
        clock,
        pace,
        record,
        llm_override,
        llm_brain,
    )
    unknown = set(agents or {}) - {resident.id for resident in game.world.residents}
    if unknown:
        raise ValueError(f"agents for unknown residents: {', '.join(sorted(unknown))}")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        clock_task = asyncio.create_task(game.run_clock())
        for resident_id, brain in (agents or {}).items():
            model = brain.model if isinstance(brain, ChatCompletionsBrain) else None
            game.start_agent(resident_id, brain, model)
        try:
            yield
        finally:
            clock_task.cancel()
            await game.stop_agents()

    app = FastAPI(title="AIverse server", lifespan=lifespan)

    @app.websocket("/ws")
    async def game_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        outbox = Outbox(outbox_limit)
        sender = asyncio.create_task(_send_all(websocket, outbox))
        receiver = asyncio.create_task(_receive_all(websocket, game, outbox))
        # Watched separately: a connection that only listens never wakes the receiver.
        overflow = asyncio.create_task(outbox.overflowed.wait())
        try:
            done, _ = await asyncio.wait({receiver, overflow}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Not awaited: waiting here could swallow a cancellation aimed at this handler.
            game.unwatch(outbox)
            for task in (sender, receiver, overflow):
                task.cancel()
        if receiver in done:
            # Re-raises anything unexpected from the receive loop.
            receiver.result()
        else:
            with suppress(WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
                await asyncio.wait_for(
                    websocket.close(CLOSE_POLICY_VIOLATION, "too many unread messages"),
                    CLOSE_TIMEOUT,
                )

    return app
