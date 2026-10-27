"""Run the server: `uv run aiverse-server [--host HOST] [--port PORT] [--duration SECONDS]`."""

import argparse
import math

import uvicorn

from .app import create_app
from .lifecycle import GAME_DURATION


def main() -> None:
    parser = argparse.ArgumentParser(description="AIverse game server")
    # Local only by default: there is no authentication or origin check yet.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--duration",
        type=float,
        default=GAME_DURATION,
        help=f"seconds until the game ends (default {GAME_DURATION:g})",
    )
    args = parser.parse_args()
    if not (math.isfinite(args.duration) and args.duration > 0):
        parser.error("--duration must be a positive number of seconds")
    uvicorn.run(create_app(duration=args.duration), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
