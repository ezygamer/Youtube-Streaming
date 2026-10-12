"""The Python rules must agree exactly with SumLink's JS rules.

Fixtures come from tools/gen_sumlink_rules_fixtures.mjs, which runs the
game's own gameLogic.js.
"""
import json
from pathlib import Path

import pytest

from games.sumlink import rules

FIXTURE = Path(__file__).parent / "fixtures" / "sumlink_rules.json"


@pytest.fixture(scope="module")
def boards():
    if not FIXTURE.exists():
        pytest.skip("Run tools/gen_sumlink_rules_fixtures.mjs first")
    return json.loads(FIXTURE.read_text())["boards"]


def test_every_pair_check_matches_js(boards):
    mismatches = []
    for n, case in enumerate(boards):
        for i, j, valid, mtype, blocking in case["checks"]:
            got = rules.is_valid_pair(case["board"], i, j)
            if got != (valid, mtype, blocking):
                mismatches.append((n, i, j, (valid, mtype, blocking), got))
    assert not mismatches, mismatches[:5]


def test_first_pair_matches_js_hint(boards):
    for case in boards:
        expected = tuple(case["first_pair"]) if case["first_pair"] else None
        assert rules.find_valid_pair(case["board"]) == expected


def test_all_pairs_match_js(boards):
    for case in boards:
        assert rules.find_valid_pairs(case["board"]) == [tuple(p) for p in case["all_pairs"]]


def test_stage_cleared_matches_js(boards):
    for case in boards:
        assert rules.is_stage_cleared(case["board"]) == case["cleared"]


def test_match_and_compact_match_js(boards):
    checked = 0
    for case in boards:
        if not case["after_first_match"]:
            continue
        i1, i2 = case["first_pair"]
        b = list(case["board"])
        b[i1] = {**b[i1], "state": "blurred"}
        b[i2] = {**b[i2], "state": "blurred"}
        new_board, removed = rules.compact_board(b)
        assert new_board == case["after_first_match"]["board"]
        assert removed == case["after_first_match"]["removed_rows"]
        assert rules.apply_match(case["board"], i1, i2) == case["after_first_match"]["board"]
        checked += 1
    assert checked > 100


def test_add_numbers_matches_js(boards):
    for case in boards:
        assert rules.add_numbers(case["board"]) == case["add_numbers"]


def test_generate_board_shape():
    import random
    rng = random.Random(1)
    for stage in (2, 3, 7):
        board = rules.generate_board(stage, rng)
        values = [c["value"] for c in board if c["state"] == "active"]
        assert len(board) == 108 and len(values) == 36 and all(1 <= v <= 9 for v in values)
    assert len(rules.active_indices(rules.generate_board(1, rng))) == 4


def test_stage1_tutorial_board():
    board = [{"value": None, "state": "cleared"}] * 108
    layout = [(3, "active"), (4, "active"), (5, "blurred"), (5, "blurred"),
              (6, "blurred"), (4, "blurred"), (4, "active"), (7, "active")]
    board = [{"value": v, "state": s} for v, s in layout] + board[8:]
    assert rules.find_valid_pair(board) == (1, 6)        # 4 and 4 across blurred cells
    assert rules.is_valid_pair(board, 0, 7)[0] is False  # 3+7 blocked by active cells
