from pathlib import Path

import pytest

from aiverse_server.agents.llm_brain import LLMSettings
from aiverse_server.server.config import Config, ConfigError, load_config

EXAMPLE = Path(__file__).parents[2] / "aiverse.example.yml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "aiverse.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_overrides_are_read_and_trimmed(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "llm:\n  override:\n    base_url: ' https://llm.example/v1 '\n    api_key: sk-secret\n",
    )
    config = load_config(path)
    assert config.llm_override() == LLMSettings("https://llm.example/v1", "sk-secret", None)
    assert "sk-secret" not in repr(config)


@pytest.mark.parametrize("text", ["", "# nothing yet\n", "llm:\n", "llm:\n  override:\n"])
def test_empty_files_and_sections_override_nothing(tmp_path: Path, text: str) -> None:
    assert load_config(write(tmp_path, text)).llm_override() == LLMSettings()


def test_the_example_overrides_nothing() -> None:
    assert load_config(EXAMPLE) == Config()


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("llm:\n  override:\n    apikey: sk-secret\n", "llm.override.apikey"),
        ("llm:\n  override:\n    api_key: ''\n", "llm.override.api_key"),
        ("llm:\n  override:\n    api_key: [sk-secret]\n", "llm.override.api_key"),
        ("llm: sk-secret\n", "llm"),
        ("- sk-secret\n", "(root)"),
        ("llm:\n  override: {api_key: sk-secret\n", "not valid YAML"),
    ],
)
def test_bad_files_name_the_problem_without_their_values(
    tmp_path: Path, text: str, problem: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, text))
    assert problem in str(raised.value)
    assert "sk-secret" not in str(raised.value)


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nowhere.yml")
