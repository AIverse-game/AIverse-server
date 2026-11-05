from pathlib import Path

import pytest

from aiverse_server.agents.llm_brain import MAX_TOKENS, LLMSettings
from aiverse_server.server.config import Config, ConfigError, load_config

EXAMPLE = (
    Path(__file__).parents[3] / "src" / "aiverse_server" / "agents" / "config" / "llm.example.yml"
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "llm.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_overrides_are_read_and_trimmed(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "override:\n  base_url: ' https://llm.example/v1 '\n  api_key: sk-secret\n",
    )
    config = load_config(path)
    assert config.llm_override() == LLMSettings("https://llm.example/v1", "sk-secret", None)
    assert "sk-secret" not in repr(config)


@pytest.mark.parametrize("text", ["", "# nothing yet\n", "override:\n"])
def test_empty_files_and_sections_override_nothing(tmp_path: Path, text: str) -> None:
    assert load_config(write(tmp_path, text)).llm_override() == LLMSettings()


def test_the_example_overrides_nothing() -> None:
    assert load_config(EXAMPLE) == Config()


def test_a_group_goes_over_the_override_for_the_residents_it_lists(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "override:\n  base_url: https://one.example/v1\n  model: one\n"
        "groups:\n  - residents: [r5, r6]\n    api_key: sk-two\n    model: two\n",
    )

    config = load_config(path)

    two = LLMSettings("https://one.example/v1", "sk-two", "two")
    assert config.resident_overrides() == {"r5": two, "r6": two}
    assert config.llm_override() == LLMSettings("https://one.example/v1", None, "one")
    assert "sk-two" not in repr(config)


def test_without_groups_no_resident_has_settings_of_their_own(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "override:\n  model: one\ngroups:\n"))

    assert config.resident_overrides() == {}


def test_a_positive_output_limit_is_read_from_the_override(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "override:\n  max_tokens: 16384\n"))

    assert config.override.max_tokens == 16384
    assert Config().override.max_tokens == MAX_TOKENS


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("override:\n  apikey: sk-secret\n", "override.apikey"),
        ("override:\n  api_key: ''\n", "override.api_key"),
        ("override:\n  api_key: [sk-secret]\n", "override.api_key"),
        ("override: sk-secret\n", "override"),
        ("llm:\n  override:\n    api_key: sk-secret\n", "llm"),
        ("- sk-secret\n", "(root)"),
        ("override: {api_key: sk-secret\n", "not valid YAML"),
        ("override:\n  max_tokens: 0\n", "override.max_tokens"),
        ("override:\n  max_tokens: -1\n", "override.max_tokens"),
        ("override:\n  max_tokens: '16384'\n", "override.max_tokens"),
        ("override:\n  max_tokens: null\n", "override.max_tokens"),
        ("groups:\n  - residents: [r1]\n    apikey: sk-secret\n", "groups.0.apikey"),
        ("groups:\n  - residents: []\n    api_key: sk-secret\n", "groups.0.residents"),
        ("groups:\n  - api_key: sk-secret\n", "groups.0.residents"),
        ("groups:\n  - residents: [r1]\n    max_tokens: 9\n", "groups.0.max_tokens"),
        (
            "groups:\n  - residents: [r1]\n    api_key: sk-secret\n  - residents: [r2, r1]\n",
            "(root): Value error, resident 'r1' is listed more than once",
        ),
        ("groups:\n  - residents: [r1, r1]\n", "resident 'r1' is listed more than once"),
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
