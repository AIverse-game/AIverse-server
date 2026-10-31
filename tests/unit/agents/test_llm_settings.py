import pytest

from aiverse_server.agents.llm_brain import LLMSettings, SettingsError, complete


def test_settings_given_override_those_chosen() -> None:
    chosen = LLMSettings("http://page", "page-key", "page-model")
    assert LLMSettings(api_key="server-key").over(chosen) == LLMSettings(
        "http://page", "server-key", "page-model"
    )
    assert LLMSettings().over(chosen) == chosen
    assert LLMSettings(model="m").given() == ("model",)
    assert "page-key" not in repr(chosen)


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        (LLMSettings(None, "k", "m"), "missing_llm_setting"),
        (LLMSettings("http://x", None, None), "missing_llm_setting"),
        (LLMSettings("ftp://x", "k", "m"), "invalid_llm_setting"),
        (LLMSettings("llm.example/v1", "k", "m"), "invalid_llm_setting"),
        (LLMSettings("http://[bad", "k", "m"), "invalid_llm_setting"),
        (LLMSettings("http://x", "sk-secret\nmore", "m"), "invalid_llm_setting"),
        (LLMSettings("http://x", "sk secret", "m"), "invalid_llm_setting"),
        (LLMSettings("http://x", "sk-密钥", "m"), "invalid_llm_setting"),
    ],
)
def test_incomplete_or_malformed_settings_are_refused(settings: LLMSettings, code: str) -> None:
    with pytest.raises(SettingsError) as raised:
        complete(settings)
    assert raised.value.code == code


def test_complete_settings_come_back_in_order() -> None:
    assert complete(LLMSettings("https://x/v1", "k", "m")) == ("https://x/v1", "k", "m")
