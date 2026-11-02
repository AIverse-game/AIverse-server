from pathlib import Path

import pytest

from aiverse_server.game.settings import GameSettings, GameSettingsError, load_game_settings

EXAMPLE = (
    Path(__file__).parents[3] / "src" / "aiverse_server" / "game" / "config" / "game.example.yml"
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "game.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_game_timing_settings_load_and_default(tmp_path: Path) -> None:
    configured = load_game_settings(
        write(
            tmp_path,
            "duration_seconds: 2400\nearly_notice_seconds: 300\nfinal_notice_seconds: 120\n",
        )
    )

    assert configured == GameSettings(
        duration_seconds=2400, early_notice_seconds=300, final_notice_seconds=120
    )
    assert load_game_settings(EXAMPLE) == GameSettings()


@pytest.mark.parametrize(
    "text",
    [
        "duration_seconds: 0\n",
        "early_notice_seconds: -1\n",
        "final_notice_seconds: 0\n",
        "duration_seconds: .inf\n",
        "duration_seconds: '2400'\n",
        "early_notice_seconds: 60\n",
        "surprise: 1\n",
    ],
)
def test_bad_game_timing_settings_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(GameSettingsError):
        load_game_settings(write(tmp_path, text))
