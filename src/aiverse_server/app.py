"""HTTP/WebSocket entry point. Each connection greets with `hello` and receives the map."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .layout import create_default_world
from .protocol import (
    PROTOCOL_VERSION,
    ErrorMessage,
    ServerMessage,
    StateMessage,
    WelcomeMessage,
    bad_message,
    encode,
    parse_client_message,
    world_view,
)
from .world import World


def handle_text(text: str, world: World) -> list[ServerMessage]:
    """Replies to one client frame. Errors leave the connection open."""
    message = parse_client_message(text)
    if isinstance(message, ErrorMessage):
        return [message]
    if message.protocol_version != PROTOCOL_VERSION:
        return [
            ErrorMessage(
                code="protocol_mismatch", message=f"server speaks protocol {PROTOCOL_VERSION}"
            )
        ]
    return [WelcomeMessage(), StateMessage(world=world_view(world))]


def create_app(world: World | None = None) -> FastAPI:
    current = world if world is not None else create_default_world()
    app = FastAPI(title="AIverse server")

    @app.websocket("/ws")
    async def game_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    return
                text = frame.get("text")
                replies = (
                    handle_text(text, current)
                    if isinstance(text, str)
                    else [bad_message("send JSON as a text frame")]
                )
                for reply in replies:
                    await websocket.send_text(encode(reply))
        except WebSocketDisconnect:
            # The client left while we were replying; nothing to clean up.
            return

    return app
