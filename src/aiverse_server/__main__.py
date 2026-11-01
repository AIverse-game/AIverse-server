"""Run the server: `uv run aiverse-server [--host HOST] [--port PORT] [--duration SECONDS]
[--seed N] [--config FILE] [--agent RESIDENT=BRAIN ...] [--log-dir DIR] [--agent-log FILE]`.

`--agent r1=scripted` drives resident r1 with the fixed-plan bot, `--agent r2=llm` or
`--agent r2=llm:MODEL` with a language model, whose base URL and API key, and model when
not given, come from the overrides in the config file, see `server/config.py`. Residents
without an agent are left to the pages, which may hand them to a model too."""

import argparse
import logging
import math
from functools import partial
from pathlib import Path

import uvicorn

from .agents.llm_brain import ChatCompletionsBrain, LLMSettings, SettingsError, complete
from .agents.runner import Brain
from .agents.scripted_brain import ScriptedBrain
from .agents.turn_logger import TurnLogger
from .game.layout import create_default_world
from .game.lifecycle import GAME_DURATION
from .server.app import create_app
from .server.config import Config, ConfigError, load_config

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

DEFAULT_CONFIG = Path(__file__).parent / "agents" / "config" / "llm.yml"
"""Read when present and no `--config` is given, wherever the server is started from."""


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
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="directory for per-resident JSONL logs, overwritten each game "
        "(default: logs/ in the server project)",
    )
    parser.add_argument(
        "--agent-log",
        default=None,
        metavar="FILE",
        help="also write every agent round to FILE as JSONL, overwritten for each game",
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
    llm_brain = partial(ChatCompletionsBrain, max_tokens=config.override.max_tokens)
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
                    f"{spec}: {error.message}; set them under override in the config file, "
                    "or the model as RESIDENT=llm:MODEL"
                )
            agents[resident_id] = llm_brain(*settings)
    # Pages can start agents later, so every resident gets a file, even without --agent.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        recorder = TurnLogger(
            args.log_dir, world.residents, Path(args.agent_log) if args.agent_log else None
        )
    except (OSError, ValueError) as error:
        parser.error(f"cannot initialize agent logs: {error}")
    with recorder:
        app = create_app(
            world,
            duration=args.duration,
            agents=agents,
            record=recorder,
            llm_override=override,
            llm_brain=llm_brain,
        )
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
