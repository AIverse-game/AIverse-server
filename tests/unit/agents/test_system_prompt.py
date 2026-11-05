from aiverse_server.agents.system_prompt import system_prompt
from aiverse_server.agents.tools import TOOLS
from aiverse_server.core.world import NOTE_LENGTH_LIMIT


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

    assert "Only coins count; energy, shards and crystals left over are worth nothing" in prompt


def test_the_prompt_encourages_selling_energy_during_the_game() -> None:
    prompt = system_prompt("甲", "a")

    assert "bring energy to the market several times during the game" in prompt


def test_the_prompt_states_what_walking_costs() -> None:
    prompt = system_prompt("甲", "a")

    assert "Walking costs 0.5 energy per map unit, rounded up per walk" in prompt


def test_the_move_tool_quotes_the_same_walking_price_as_the_prompt() -> None:
    move = next(tool for tool in TOOLS if tool.name == "move_to")

    assert "Costs 0.5 energy per map unit" in move.description
