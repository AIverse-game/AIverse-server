import pytest

from aiverse_server.__main__ import parse_agent


@pytest.mark.parametrize(
    ("text", "agent"),
    [
        ("r1=scripted", ("r1", "scripted", None)),
        ("r2=llm", ("r2", "llm", None)),
        ("r2=llm:nvidia/x:free", ("r2", "llm", "nvidia/x:free")),
    ],
)
def test_agents_are_named_resident_equals_brain(
    text: str, agent: tuple[str, str, str | None]
) -> None:
    assert parse_agent(text) == agent


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("r1", "expected RESIDENT=scripted"),
        ("=scripted", "expected RESIDENT=scripted"),
        ("r1=gpt", "expected RESIDENT=scripted"),
        ("r1=openrouter", "expected RESIDENT=scripted"),
        ("r1=scripted:x", "the scripted brain takes no model"),
    ],
)
def test_agents_named_otherwise_are_refused(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_agent(text)
