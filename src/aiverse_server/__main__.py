"""Run the server: `uv run aiverse-server [--host HOST] [--port PORT] [--duration SECONDS]
[--seed N] [--agent RESIDENT=BRAIN ...] [--agent-log FILE]`.

`--agent r1=scripted` drives resident r1 with the fixed-plan bot, `--agent
r2=openrouter` or `--agent r2=openrouter:MODEL` with a model on OpenRouter, whose API key is
read from the environment variable `OPENROUTER_API_KEY`. Residents without an agent are left
to the pages."""

import argparse
import logging
import math
import os
from collections.abc import Callable
from typing import TextIO

import uvicorn

from .agents.openrouter import DEFAULT_MODEL, OpenRouterBrain
from .agents.runner import Brain, TurnRecord, record_to_json, summarize
from .agents.scripted import ScriptedBrain
from .game.layout import create_default_world
from .game.lifecycle import GAME_DURATION
from .server.app import create_app

API_KEY_VARIABLE = "OPENROUTER_API_KEY"


def parse_agent(spec: str) -> tuple[str, str, str | None]:
    """`RESIDENT=scripted` or `RESIDENT=openrouter[:MODEL]` as (resident, brain, model)."""
    resident_id, sep, brain = spec.partition("=")
    kind, _, model = brain.partition(":")
    if not sep or not resident_id or kind not in ("scripted", "openrouter"):
        raise ValueError(f"expected RESIDENT=scripted or RESIDENT=openrouter[:MODEL], got {spec!r}")
    if kind == "scripted" and model:
        raise ValueError(f"the scripted brain takes no model: {spec!r}")
    return resident_id, kind, model or None


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
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed for where the token pools go (default: different every game)",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="RESIDENT=BRAIN",
        help="drive a resident with an agent: scripted, or openrouter[:MODEL] "
        f"(default model {DEFAULT_MODEL}; needs {API_KEY_VARIABLE}); may be repeated",
    )
    parser.add_argument(
        "--agent-log",
        default=None,
        metavar="FILE",
        help="append every agent round to FILE as one line of JSON",
    )
    args = parser.parse_args()
    if not (math.isfinite(args.duration) and args.duration > 0):
        parser.error("--duration must be a positive number of seconds")
    world = create_default_world(args.seed)
    resident_ids = {resident.id for resident in world.residents}
    agents: dict[str, Brain] = {}
    for spec in args.agent:
        try:
            resident_id, kind, model = parse_agent(spec)
        except ValueError as error:
            parser.error(str(error))
        if resident_id not in resident_ids:
            parser.error(
                f"no resident {resident_id!r}; choose from {', '.join(sorted(resident_ids))}"
            )
        if resident_id in agents:
            parser.error(f"resident {resident_id!r} is given two agents")
        if kind == "scripted":
            agents[resident_id] = ScriptedBrain(seed=args.seed)
        else:
            api_key = os.environ.get(API_KEY_VARIABLE, "").strip()
            if not api_key:
                parser.error(f"set {API_KEY_VARIABLE} to use an OpenRouter agent")
            agents[resident_id] = OpenRouterBrain(api_key, model or DEFAULT_MODEL)
    log_file = open(args.agent_log, "a", encoding="utf-8") if args.agent_log else None  # noqa: SIM115
    if agents:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = create_app(world, duration=args.duration, agents=agents, record=_recorder(log_file))
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        if log_file is not None:
            log_file.close()


def _recorder(log_file: TextIO | None) -> Callable[[TurnRecord], None]:
    logger = logging.getLogger("aiverse_server.agents")

    def record(turn: TurnRecord) -> None:
        logger.info("%s", summarize(turn))
        if log_file is not None:
            log_file.write(record_to_json(turn) + "\n")
            log_file.flush()

    return record


if __name__ == "__main__":
    main()
