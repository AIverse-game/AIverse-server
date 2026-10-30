"""Run the server: `uv run aiverse-server [--host HOST] [--port PORT] [--duration SECONDS]
[--seed N] [--config FILE] [--agent RESIDENT=BRAIN ...] [--agent-log FILE]`.

`--agent r1=scripted` drives resident r1 with the fixed-plan bot, `--agent r2=llm` or
`--agent r2=llm:MODEL` with a language model, whose base URL and API key, and model when
not given, come from the overrides in the config file, see `server/config.py`. Residents
without an agent are left to the pages, which may hand them to a model too."""

import argparse
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import uvicorn

from .agents.llm import ChatCompletionsBrain, LLMSettings, SettingsError, complete
from .agents.runner import Brain, TurnRecord, record_to_json, summarize
from .agents.scripted import ScriptedBrain
from .game.layout import create_default_world
from .game.lifecycle import GAME_DURATION
from .server.app import create_app
from .server.config import Config, ConfigError, load_config

DEFAULT_CONFIG = Path("aiverse.yml")
"""Read when present and no `--config` is given."""


def parse_agent(spec: str) -> tuple[str, str, str | None]:
    """`RESIDENT=scripted` or `RESIDENT=llm[:MODEL]` as (resident, brain, model)."""
    resident_id, sep, brain = spec.partition("=")
    kind, _, model = brain.partition(":")
    if not sep or not resident_id or kind not in ("scripted", "llm"):
        raise ValueError(f"expected RESIDENT=scripted or RESIDENT=llm[:MODEL], got {spec!r}")
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
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help=f"YAML config file (default {DEFAULT_CONFIG} when present)",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="RESIDENT=BRAIN",
        help="drive a resident with an agent: scripted, or llm[:MODEL] with the base URL and "
        "API key from the config file; may be repeated",
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
    config_path: Path | None = args.config
    if config_path is None and DEFAULT_CONFIG.is_file():
        config_path = DEFAULT_CONFIG
    try:
        config = load_config(config_path) if config_path is not None else Config()
    except ConfigError as error:
        parser.error(str(error))
    override = config.llm_override()
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
            try:
                settings = complete(override.over(LLMSettings(model=model)))
            except SettingsError as error:
                parser.error(
                    f"{spec}: {error.message}; set them under llm.override in the config file, "
                    "or the model as RESIDENT=llm:MODEL"
                )
            agents[resident_id] = ChatCompletionsBrain(*settings)
    log_file = open(args.agent_log, "a", encoding="utf-8") if args.agent_log else None  # noqa: SIM115
    # Pages may start agents at any time, so their rounds are always shown.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Its request lines carry the base URL, which a player may have put credentials in.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(
        world,
        duration=args.duration,
        agents=agents,
        record=_recorder(log_file),
        llm_override=override,
    )
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
