"""The server's config file, in YAML; see `aiverse.example.yml` at the repository root.

So far it holds one thing, overrides for the language model agents ask:

```yaml
llm:
  override:
    base_url: https://openrouter.ai/api/v1
    api_key: sk-...
    model: nvidia/nemotron-3.5-lightning
```

Each setting given replaces what the player chose on the page, or with `--agent
RESIDENT=llm[:MODEL]` on the command line, for every resident a model drives; those left out
come from the player. The file may hold a key, so it stays out of the repository. Unknown
keys are refused, so a misspelt setting is not silently ignored.
"""

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from ..agents.llm import LLMSettings

Setting = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_empty(cls, data: Any) -> Any:
        """A key with nothing after it, such as a section whose settings are all commented
        out, is null in YAML; take it as left out."""
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class LLMOverride(_Section):
    base_url: Setting | None = None
    api_key: Setting | None = Field(default=None, repr=False)
    model: Setting | None = None


class LLMSection(_Section):
    override: LLMOverride = LLMOverride()


class Config(_Section):
    llm: LLMSection = LLMSection()

    def llm_override(self) -> LLMSettings:
        override = self.llm.override
        return LLMSettings(override.base_url, override.api_key, override.model)


class ConfigError(Exception):
    pass


def load_config(path: Path) -> Config:
    """Reads the file at `path`; an empty file is the default config. Raises `ConfigError`
    whose message names the problem but never repeats a value, which may be a key."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise ConfigError(f"cannot read {path}: {reason}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{path} is not valid YAML{where}") from None
    try:
        return Config.model_validate({} if data is None else data)
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '(root)'}: {issue['msg']}"
            for issue in error.errors(include_input=False)
        )
        raise ConfigError(f"{path}: {issues}") from None
