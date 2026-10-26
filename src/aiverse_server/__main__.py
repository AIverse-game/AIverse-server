"""Run the server: `uv run aiverse-server [--host HOST] [--port PORT]`."""

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="AIverse game server")
    # Local only by default: there is no authentication or origin check yet.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
