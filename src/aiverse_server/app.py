"""HTTP/WebSocket entry point.

Every connection greets with `hello` and receives the map. After that it may send operation
primitives; when one changes the world, every greeted connection receives the new `state`.
The game ends a fixed time after the app is created; every connection then receives the
final `state`. Mining jobs finish on the same clock, and their payout is broadcast too.
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .actions import ActionRejected, perform
from .layout import create_default_world
from .lifecycle import GAME_DURATION, end_game
from .mining import finish_mining, next_finish
from .protocol import (
    PROTOCOL_VERSION,
    ActionResultMessage,
    ErrorMessage,
    HelloMessage,
    ServerMessage,
    StateMessage,
    WelcomeMessage,
    action_rejected,
    bad_message,
    encode,
    parse_client_message,
    to_action,
    world_view,
)
from .world import World


@dataclass(frozen=True)
class Outcome:
    """What one client frame leads to."""

    replies: list[ServerMessage]
    """Sent back to the sender only."""
    world: World
    """The world afterwards; a different object only when an action changed it."""
    greeted: bool
    """Whether the sender has completed the handshake and may act."""


def handle_text(text: str, world: World, greeted: bool, remaining_seconds: float) -> Outcome:
    """Handles one client frame. Errors leave the connection open and the world unchanged.
    `remaining_seconds` is the game time left, for the state that answers `hello`."""
    message = parse_client_message(text)
    if isinstance(message, ErrorMessage):
        return Outcome([message], world, greeted)
    if isinstance(message, HelloMessage):
        if message.protocol_version != PROTOCOL_VERSION:
            mismatch = ErrorMessage(
                code="protocol_mismatch", message=f"server speaks protocol {PROTOCOL_VERSION}"
            )
            return Outcome([mismatch], world, greeted)
        state = StateMessage(world=world_view(world, remaining_seconds))
        return Outcome([WelcomeMessage(), state], world, True)
    if not greeted:
        return Outcome([bad_message("send hello before any action")], world, greeted)
    try:
        after = perform(world, to_action(message))
    except ActionRejected as rejection:
        return Outcome([action_rejected(rejection)], world, greeted)
    return Outcome([ActionResultMessage(ok=True)], after, greeted)


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
    """

    def __init__(self, limit: int = OUTBOX_LIMIT) -> None:
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
        # Set on every broadcast: a new mining job may end before what the timer waits for.
        self._changed = asyncio.Event()

    def remaining(self) -> float:
        return max(self._duration - (self._clock() - self._start), 0.0)

    def tick(self) -> None:
        """Bring the world up to now: move its time on, pay out the mining jobs that are
        done, end the game if its time is up, and tell everyone if anything changed. Called
        by the timer and before each frame, so no action slips in late while the timer waits
        for its turn."""
        if self.world.ended:
            return
        # One reading decides both, so the deadline cannot pass between paying and ending.
        elapsed = self._clock() - self._start
        # Capped at the end, so jobs due after the deadline never pay out.
        timed = replace(self.world, time=max(min(elapsed, self._duration), self.world.time))
        after = finish_mining(timed)
        if elapsed >= self._duration:
            after = end_game(after)
        self.world = after
        if after is not timed:
            self.broadcast_world()

    def _until_next(self) -> float:
        """Seconds until the game ends or the next mining job does, whichever comes first."""
        wait = self.remaining()
        finish = next_finish(self.world)
        if finish is not None:
            wait = min(wait, finish - (self._clock() - self._start))
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

    def broadcast(self, message: StateMessage) -> None:
        text = encode(message)
        for outbox in self._outboxes:
            outbox.put_state(text)
        self._changed.set()

    def broadcast_world(self) -> None:
        self.broadcast(StateMessage(world=world_view(self.world, self.remaining())))


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
            outcome = handle_text(text, game.world, greeted, game.remaining())
            changed = outcome.world is not game.world
            game.world = outcome.world
            if outcome.greeted and not greeted:
                game.watch(outbox)
            greeted = outcome.greeted
            for reply in outcome.replies:
                # The state answering hello also waits in the slot, behind the welcome.
                if isinstance(reply, StateMessage):
                    outbox.put_state(encode(reply))
                else:
                    outbox.put(encode(reply))
            if changed:
                game.broadcast_world()
    except WebSocketDisconnect:
        # The client left mid-receive; nothing else to clean up.
        return


def create_app(
    world: World | None = None,
    outbox_limit: int = OUTBOX_LIMIT,
    duration: float = GAME_DURATION,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """The game starts now, when its world is created, and ends `duration` seconds later."""
    game = Game(world if world is not None else create_default_world(), duration, clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timer = asyncio.create_task(game.run_clock())
        try:
            yield
        finally:
            timer.cancel()

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
