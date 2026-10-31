import pytest

from aiverse_server.__main__ import parse_agent


def test_agents_are_named_resident_equals_brain() -> None:
    assert parse_agent("r1=scripted") == ("r1", "scripted", None)
    assert parse_agent("r2=llm") == ("r2", "llm", None)
    assert parse_agent("r2=llm:nvidia/x:free") == ("r2", "llm", "nvidia/x:free")
    for bad in ("r1", "=scripted", "r1=gpt", "r1=scripted:x", "r1=openrouter"):
        with pytest.raises(ValueError):
            parse_agent(bad)
