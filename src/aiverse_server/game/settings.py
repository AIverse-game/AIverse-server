"""Validated timing settings for one game, loaded from ``game/config/game.yml``."""

import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .lifecycle import EARLY_ENDING_NOTICE_SECONDS, ENDING_NOTICE_SECONDS, GAME_DURATION


class GameSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_seconds: float = Field(default=GAME_DURATION, strict=True, gt=0)
    early_notice_seconds: float = Field(default=EARLY_ENDING_NOTICE_SECONDS, strict=True, gt=0)
    final_notice_seconds: float = Field(default=ENDING_NOTICE_SECONDS, strict=True, gt=0)

    @model_validator(mode="after")
    def check_notice_order(self) -> "GameSettings":
        if not all(
            math.isfinite(value)
            for value in (
                self.duration_seconds,
                self.early_notice_seconds,
                self.final_notice_seconds,
            )
        ):
            raise ValueError("game timing values must be finite")
        if self.early_notice_seconds <= self.final_notice_seconds:
            raise ValueError("early_notice_seconds must exceed final_notice_seconds")
        return self


class GameSettingsError(Exception):
    pass


def load_game_settings(path: Path) -> GameSettings:
    """Read a YAML timing file; missing fields use the game's defaults."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise GameSettingsError(f"cannot read {path}: {reason}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise GameSettingsError(f"{path} is not valid YAML{where}") from None
    try:
        return GameSettings.model_validate({} if data is None else data)
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '(root)'}: {issue['msg']}"
            for issue in error.errors(include_input=False)
        )
        raise GameSettingsError(f"{path}: {issues}") from None
