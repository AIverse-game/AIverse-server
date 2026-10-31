"""Per-resident JSONL files for the current game, overwritten when the server starts."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

from ..core.world import Resident
from .runner import TurnRecord, record_to_json, summarize

logger = logging.getLogger(__name__)


class TurnLogger:
    """One owner of the game's log files; restarting an agent never truncates its file."""

    def __init__(
        self, directory: Path, residents: Sequence[Resident], combined: Path | None = None
    ) -> None:
        paths = {
            person.id: directory / f"{quote(person.id, safe='')}.jsonl" for person in residents
        }
        if combined is not None and combined.resolve() in {
            path.resolve() for path in paths.values()
        }:
            raise ValueError("combined log must differ from each resident's log")
        directory.mkdir(parents=True, exist_ok=True)
        self._stack = ExitStack()
        self._files: dict[str, TextIO] = {}
        self._names = {person.id: person.name for person in residents}
        self._rounds = dict.fromkeys(paths, 0)
        try:
            for resident_id, path in paths.items():
                self._files[resident_id] = self._stack.enter_context(
                    path.open("w", encoding="utf-8")
                )
            self._combined = (
                self._stack.enter_context(combined.open("w", encoding="utf-8"))
                if combined
                else None
            )
        except BaseException:
            self._stack.close()
            raise

    def __enter__(self) -> TurnLogger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stack.close()

    def __call__(self, turn: TurnRecord) -> None:
        resident_id = turn.resident_id
        number = self._rounds[resident_id] + 1
        data = json.loads(record_to_json(turn))
        data["round"] = number
        data["resident_name"] = self._names[resident_id]
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        stream = self._files[resident_id]
        stream.write(line)
        stream.flush()
        self._rounds[resident_id] = number
        if self._combined is not None:
            self._combined.write(line)
            self._combined.flush()
        logger.info("%s", summarize(turn))
