"""HTTP/WebSocket entry point.

Every connection greets with `hello` and receives the map. After that it may send operation
primitives; when one changes the world, every greeted connection receives the new `state`.
A connection may choose a resident to view as with `view_as`; its states then carry that
resident's whispers, and no one else's.
The game ends a fixed time after the app is created; every connection then receives the
final `state`. Mining jobs and walks finish on the same clock, and are broadcast too.
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..agents.runner import DEFAULT_PACE, AgentRunner, Brain, Pace, TurnRecord
from ..core.world import Note, NoteTerm, World
from ..game.actions import Action, ActionRejected, perform
from ..game.clock import next_due, settle_due
from ..game.layout import create_default_world
from ..game.lifecycle import GAME_DURATION, end_game
from ..residents.memory import forget, remember
from ..residents.perception import Perception, begin_turn
from .protocol import (
    PROTOCOL_VERSION,
    ActionResultMessage,
    ErrorMessage,
    HelloMessage,
    ServerMessage,
    StateMessage,
    ViewAsMessage,
    WelcomeMessage,
    action_rejected,
    bad_message,
    encode,
    parse_client_message,
    to_action,
    world_view,
)


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
    """The resident the sender views as, whose whispers its states carry; None for nobody.
    No default: every outcome must carry it on, or the connection's choice would be lost."""


def handle_text(
    text: str,
    world: World,
    greeted: bool,
    remaining_seconds: float,
    viewer: str | None = None,
) -> Outcome:
    """Handles one client frame. Errors leave the connection open and the world unchanged.
    `remaining_seconds` is the game time left, and `viewer` the resident the sender views as,
    for the states that answer `hello` and `view_as`."""
    message = parse_client_message(text)
    if isinstance(message, ErrorMessage):
        return Outcome([message], world, greeted, viewer)
    if isinstance(message, HelloMessage):
        if message.protocol_version != PROTOCOL_VERSION:
            mismatch = ErrorMessage(
                code="protocol_mismatch", message=f"server speaks protocol {PROTOCOL_VERSION}"
            )
            return Outcome([mismatch], world, greeted, viewer)
        state = StateMessage(world=world_view(world, remaining_seconds, viewer))
        return Outcome([WelcomeMessage(), state], world, True, viewer)
    if not greeted:
        return Outcome([bad_message("send hello before anything else")], world, greeted, viewer)
    if isinstance(message, ViewAsMessage):
        chosen = message.resident_id
        if chosen is not None and all(resident.id != chosen for resident in world.residents):
            return Outcome([bad_message(f"no resident with id {chosen!r}")], world, greeted, viewer)
        state = StateMessage(world=world_view(world, remaining_seconds, chosen))
        return Outcome([state], world, greeted, chosen)
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
    `viewer` is the resident the connection views as, which decides the whispers in its
    snapshots.
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


class Game:
    """The world all connections share, its clock, and the outboxes of those that said hello.

    A broadcast reaches every outbox at once, so no connection can see an older state
    after a newer one.
    """

    def __init__(
        self,
        world: World,
        duration: float = GAME_DURATION,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.world = world
        self._clock = clock
        self._start = clock()
        self._duration = duration
        self._outboxes: set[Outbox] = set()
        # Set on every broadcast: a new job or walk may end before what the timer waits for.
        self._changed = asyncio.Event()
        # One per agent waiting for something to happen, see `agents/runner.py`.
        self._subscribers: set[asyncio.Event] = set()

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

    def _until_next(self) -> float:
        """Seconds until the game ends or the next mining job or walk does, whichever comes
        first."""
        wait = self.remaining()
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
        per viewer, since only the whispers differ."""
        remaining = self.remaining()
        texts: dict[str | None, str] = {}
        for outbox in self._outboxes:
            if outbox.viewer not in texts:
                view = world_view(self.world, remaining, outbox.viewer)
                texts[outbox.viewer] = encode(StateMessage(world=view))
            outbox.put_state(texts[outbox.viewer])
        self._changed.set()
        for changed in self._subscribers:
            changed.set()


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
            outcome = handle_text(text, game.world, greeted, game.remaining(), outbox.viewer)
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
) -> FastAPI:
    """The game starts now, when its world is created, and ends `duration` seconds later.
    `agents` maps resident ids to the brains that drive them while the app runs, see
    `agents/runner.py`; `record` receives every round they take. Other residents are left to
    the pages."""
    game = Game(world if world is not None else create_default_world(), duration, clock)
    unknown = set(agents or {}) - {resident.id for resident in game.world.residents}
    if unknown:
        raise ValueError(f"agents for unknown residents: {', '.join(sorted(unknown))}")
    runners = [
        AgentRunner(game, resident_id, brain, pace, record)
        for resident_id, brain in (agents or {}).items()
    ]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(game.run_clock())]
        tasks += [asyncio.create_task(runner.run()) for runner in runners]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

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
