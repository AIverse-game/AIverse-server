import json
from pathlib import Path

import pytest

from aiverse_server.agents.runner import TurnRecord
from aiverse_server.agents.turn_logger import TurnLogger
from aiverse_server.core.world import Position, Resident

RESIDENTS = (
    Resident("r1", "阿青", "#112233", Position(0, 0)),
    Resident("r2", "小麦", "#445566", Position(1, 1)),
)


def turn(resident_id: str = "r1", remark: str = "等待") -> TurnRecord:
    return TurnRecord(resident_id, 10, '{"long_term_notes":[]}', (), remark, ended_at=12)


def test_a_new_game_overwrites_only_its_residents_files(tmp_path: Path) -> None:
    (tmp_path / "r1.jsonl").write_text("old game", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")

    with TurnLogger(tmp_path, RESIDENTS) as record:
        record(turn())

    rows = (tmp_path / "r1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["remark"] == "等待"
    assert (tmp_path / "r2.jsonl").read_text() == ""
    assert (tmp_path / "notes.txt").read_text() == "keep"


def test_each_resident_has_separate_round_numbers_and_immediately_readable_records(
    tmp_path: Path,
) -> None:
    with TurnLogger(tmp_path, RESIDENTS) as record:
        record(turn("r1", "第一轮"))
        record(turn("r2", "另一角色"))
        record(turn("r1", "第二轮"))
        # Read before close: each round must already be flushed.
        a = [json.loads(row) for row in (tmp_path / "r1.jsonl").read_text().splitlines()]
        b = [json.loads(row) for row in (tmp_path / "r2.jsonl").read_text().splitlines()]

    assert [row["round"] for row in a] == [1, 2]
    assert [row["resident_id"] for row in a] == ["r1", "r1"]
    assert [row["round"] for row in b] == [1]
    assert b[0]["resident_name"] == "小麦"


def test_the_optional_combined_file_is_also_overwritten(tmp_path: Path) -> None:
    combined = tmp_path / "all.jsonl"
    combined.write_text("old game", encoding="utf-8")

    with TurnLogger(tmp_path / "roles", RESIDENTS, combined) as record:
        record(turn("r1"))
        record(turn("r2"))

    assert [json.loads(row)["resident_id"] for row in combined.read_text().splitlines()] == [
        "r1",
        "r2",
    ]


def test_a_combined_path_cannot_alias_a_resident_log(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="combined log must differ"):
        TurnLogger(tmp_path, RESIDENTS, tmp_path / "r1.jsonl")
