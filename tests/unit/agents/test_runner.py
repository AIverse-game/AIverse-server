import json

from aiverse_server.agents.observation import Observation, observe, to_json
from aiverse_server.agents.runner import TurnRecord, record_to_json
from aiverse_server.agents.tools import ResidentTools
from aiverse_server.core.world import Inventory, Position, Resident, World, WorldMap
from aiverse_server.server.app import Game

POSTER = "poster"
WORLD = World(
    map=WorldMap(size=100),
    residents=(
        Resident("a", "甲", "#112233", Position(50, 50), Inventory(((POSTER, 100),)), coins=5000),
        Resident("b", "乙", "#445566", Position(60, 50), Inventory(((POSTER, 100),)), coins=5000),
    ),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_game(world: World = WORLD, duration: float = 600) -> tuple[Game, FakeClock]:
    clock = FakeClock()
    return Game(world, duration, clock), clock


def look(game: Game, resident_id: str) -> Observation:
    # The turn first: it brings the world up to now.
    perception = game.begin_turn(resident_id)
    return observe(game.world, resident_id, perception, game.remaining())


def test_records_are_standard_json_even_with_nan_arguments() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    tools.call("move_to", {"x": float("nan"), "y": 1})
    turn = TurnRecord("a", 0.0, to_json(look(game, "a")), tuple(tools.calls))
    line = record_to_json(turn)
    assert json.loads(line)["calls"][0]["arguments"]["x"] == "nan"


def test_action_balances_count_escrow_as_owned_resources() -> None:
    game, _ = make_game()
    tools = ResidentTools(game, "a")

    tools.call("offer_to_buy", {"seller_id": "b", "kind": POSTER, "quantity": 10, "price": 100})
    tools.buy_parcel()
    tools.move_to(60, 50)
    tools.buy_parcel()

    calls = tools.calls
    assert calls[0].coins_change == 0  # Reserving 100 coins does not lower ranked wealth.
    assert calls[1].coins_change == -2790
    assert calls[2].coins_change == 0  # Walking costs time, not coins.
    assert calls[3].result.ok is False
    assert calls[3].coins_change == 0


def test_a_payment_that_fell_due_is_not_attributed_to_the_next_action() -> None:
    game, clock = make_game()
    tools = ResidentTools(game, "a")
    clock.now = 22

    tools.say("hello")

    assert tools.calls[0].time == 22
    assert tools.calls[0].coins_change == 0


def test_page_history_is_bounded_and_has_no_full_observation() -> None:
    from aiverse_server.agents.tools import ResourceBalance
    from aiverse_server.server.protocol import AGENT_TURN_LIMIT, world_view

    game, _ = make_game()
    balance = ResourceBalance(5000)
    turn = TurnRecord("a", 0, "private observation", (), "等待", before=balance, after=balance)

    for _ in range(AGENT_TURN_LIMIT + 5):
        game.record_turn(turn)
    shown = world_view(game.world, 100, "a", agent_turns=game.agent_turns)

    assert shown.agent_timeline is not None
    assert len(shown.agent_timeline.turns) == AGENT_TURN_LIMIT
    assert shown.agent_timeline.turns[0].seq == 6
    assert "private observation" not in shown.model_dump_json()


def test_page_parameters_are_redacted_before_escaping_or_truncation() -> None:
    import ast

    from aiverse_server.agents.llm_brain import LLMSettings, complete
    from aiverse_server.agents.tools import ResourceBalance
    from aiverse_server.server.protocol import agent_turn_view

    key = "key\\secret'quoted"
    complete(LLMSettings("https://x/v1", key, "m"))
    game, _ = make_game()
    tools = ResidentTools(game, "a")
    balance = ResourceBalance(5000)
    tools.call("unknown_tool", {key: [key, {"nested": key}]})
    # A truncated raw argument cannot safely be shown after substring-based redaction.
    tools.call("say", "x" * 295 + key)
    turn = TurnRecord("a", 0, "{}", tuple(tools.calls), before=balance, after=balance)

    shown = agent_turn_view(turn, 1, lambda text: text.replace(key, "***"))

    assert ast.literal_eval(shown.calls[0].arguments) == {"***": ["***", {"nested": "***"}]}
    assert shown.calls[1].arguments == "参数不是有效 JSON，未展示原文。"
    assert "key" not in shown.model_dump_json()


def test_redacting_a_short_key_preserves_log_routing_and_schema_fields() -> None:
    from aiverse_server.agents.runner import redact_turn

    turn = TurnRecord(
        "jack", 0, '{"me":{"id":"jack","coins":3},"short_term_notes":[{"text":"k"}]}', (), "k"
    )

    shown = redact_turn(turn, lambda text: text.replace("k", "***"))
    observation = json.loads(shown.observation)

    assert shown.resident_id == "jack"
    assert observation["me"]["coins"] == 3
    assert observation["short_term_notes"][0]["text"] == "***"
    assert shown.remark == "***"
