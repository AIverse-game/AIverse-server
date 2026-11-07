from aiverse_server.agents.system_prompt import system_prompt
from aiverse_server.agents.tools import TOOLS
from aiverse_server.core.world import NOTE_LENGTH_LIMIT


def test_the_base_prompt_stays_compact() -> None:
    prompt = system_prompt("甲", "a")

    assert len(prompt) < 6000


def test_the_prompt_tells_a_model_that_thinking_time_is_game_time() -> None:
    prompt = system_prompt("甲", "a")

    assert "The game clock keeps running while you think" in prompt


def test_the_prompt_names_the_thinking_time_fields_of_the_observation() -> None:
    prompt = system_prompt("甲", "a")

    assert "last_thinking_seconds" in prompt and "avg_thinking_seconds" in prompt


def test_the_prompt_says_a_longer_note_is_refused() -> None:
    prompt = system_prompt("甲", "a")

    assert f"at most {NOTE_LENGTH_LIMIT} characters; a longer one is refused" in prompt


def test_the_prompt_says_only_coins_count_at_the_end() -> None:
    prompt = system_prompt("甲", "a")

    assert "Only coins count; land and items are worth nothing at the end." in prompt


def test_the_prompt_says_nothing_pays_coins_yet() -> None:
    prompt = system_prompt("甲", "a")

    assert "nothing in the world pays coins" in prompt
    assert "buy_parcel" in prompt


def test_the_prompt_names_none_of_the_removed_mechanics() -> None:
    prompt = system_prompt("甲", "a").lower()

    removed = (
        "compute",
        "spring",
        "harvest",
        "collector",
        "program",
        "studio",
        "material",
        "market",
        "mine",
        "gather",
        "hosting",
        "meeting room",
        "ad slot",
    )
    assert not [word for word in removed if word in prompt]


def test_the_prompt_states_that_walking_costs_time_only() -> None:
    prompt = system_prompt("甲", "a")

    assert "straight line at 2.5 units/s and costs time only" in prompt


def test_the_move_tool_quotes_the_same_walking_speed_as_the_prompt() -> None:
    move = next(tool for tool in TOOLS if tool.name == "move_to")

    assert "costs time only" in move.description
    assert "2.5 map units per second" in move.description
