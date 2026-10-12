"""BotRunner flow tests with a scripted fake game (no browser)."""
import asyncio

import pytest

from core.bot_runner import BotRunner, BotStoppedError, RunnerSettings
from core.events import EventBus
from core.game_adapter import GameAdapter, GameState, StepResult
from core.stats_db import StatsDB


class FakeGame(GameAdapter):
    """Scores 10 per step, levels up every 5 steps, game over after `moves_per_life` steps."""

    def __init__(self, moves_per_life=12, fail_steps=(), can_continue=True):
        super().__init__(page=None, base_url="", config={})
        self.moves_per_life = moves_per_life
        self.fail_steps = set(fail_steps)
        self.can_continue = can_continue
        self.score = self.moves = self.step_calls = 0
        self.restarts = self.continues = self.opens = 0

    async def open(self):
        self.opens += 1

    async def restart(self):
        self.restarts += 1
        self.score = self.moves = 0

    async def read_state(self):
        return GameState(score=self.score, level=1 + self.score // 50,
                         game_over=self.moves >= self.moves_per_life)

    async def is_game_over(self):
        return self.moves >= self.moves_per_life

    async def step(self):
        self.step_calls += 1
        if self.step_calls in self.fail_steps:
            raise RuntimeError("click failed")
        if self.moves >= self.moves_per_life:
            return StepResult("game_over")
        self.moves += 1
        self.score += 10
        return StepResult("scored")

    async def continue_after_game_over(self):
        if not self.can_continue:
            return False
        self.continues += 1
        self.moves = 0  # score kept
        return True


def make_runner(game, mode, tmp_path, **settings):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    db = StatsDB(tmp_path / "stats.db")
    settings = RunnerSettings(restart_delay_s=0, error_backoff_s=(0,), **settings)
    runner = BotRunner(game, "fake", "Fake", mode.get("name", "normal"), mode, bus, db, settings)
    return runner, events, db


def types(events):
    return [e.type for e in events]


def run(coro):
    return asyncio.run(coro)


def test_restart_after_game_over_and_high_score(tmp_path):
    game = FakeGame(moves_per_life=12)
    runner, events, db = make_runner(game, {"on_game_over": "restart"}, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=2))

    assert game.restarts == 2
    assert types(events).count("game_over") == 2
    assert db.high_score("fake", "normal") == 120
    # Run 2 ties run 1 (120) - a tie is not a new record.
    assert "new_high_score" not in types(events)
    assert "level_up" in types(events)


def test_new_high_score_only_when_beating_existing_record(tmp_path):
    db = StatsDB(tmp_path / "stats.db")
    rid = db.start_run("fake", "normal")
    db.end_run(rid, score=50, max_level=1, moves=5, max_combo=5, end_reason="game_over", duration_s=1)
    db.close()
    runner, events, _ = make_runner(FakeGame(moves_per_life=12), {"on_game_over": "restart"}, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=1))
    highs = [e for e in events if e.type == "new_high_score"]
    assert len(highs) == 1 and highs[0].data["score"] == 60 and highs[0].data["previous"] == 50


def test_combo_milestones(tmp_path):
    runner, events, _ = make_runner(FakeGame(moves_per_life=12), {}, tmp_path, combo_milestones=(5, 10))
    run(runner.run_forever(asyncio.Event(), max_runs=1))
    assert [e.data["combo"] for e in events if e.type == "combo"] == [5, 10]


def test_endless_continues_and_keeps_score(tmp_path):
    game = FakeGame(moves_per_life=5)
    runner, events, _ = make_runner(game, {"on_game_over": "continue"}, tmp_path)
    stop = asyncio.Event()

    async def go():
        async def stopper():
            while game.continues < 3:
                await asyncio.sleep(0)
            stop.set()
        await asyncio.gather(runner.run_forever(stop), stopper())

    run(go())
    assert game.restarts == 1            # never restarted after the first start
    assert game.score >= 150             # score kept across continues
    assert [e.data["continuing"] for e in events if e.type == "game_over"] == [True, True, True]


def test_challenge_completes_and_rotates(tmp_path):
    mode = {"on_game_over": "restart", "challenges": [
        {"name": "A", "goal": {"score": 30}},
        {"name": "B", "goal": {"level": 2}},
    ]}
    runner, events, _ = make_runner(FakeGame(moves_per_life=100), mode, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=2))
    done = [e.data["challenge"] for e in events if e.type == "challenge_completed"]
    assert done == ["A", "B"]


def test_speed_run_goal(tmp_path):
    runner, events, db = make_runner(FakeGame(moves_per_life=100), {"goal": {"level": 3}}, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=1))
    assert "goal_reached" in types(events)
    assert runner.run.score == 100       # level 3 reached at score 100


def test_levels_cleared_goal_waits_for_next_level(tmp_path):
    # levels_cleared: 2 is met only once level 3 starts (score 100 in FakeGame).
    runner, events, db = make_runner(FakeGame(moves_per_life=100), {"goal": {"levels_cleared": 2}}, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=1))
    goal = [e for e in events if e.type == "goal_reached"][0]
    assert goal.data["level"] == 3 and goal.data["goal_level"] == 2
    assert runner.status()["goal_level"] == 2


def test_recovers_from_occasional_errors(tmp_path):
    game = FakeGame(moves_per_life=6, fail_steps={2, 4})
    runner, events, _ = make_runner(game, {}, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=1))
    assert types(events).count("bot_error") == 2
    assert game.opens == 1 + 2           # initial open + one reload per error
    assert "game_over" in types(events)


def test_gives_up_after_too_many_consecutive_errors(tmp_path):
    game = FakeGame(fail_steps=set(range(1, 100)))
    runner, events, _ = make_runner(game, {}, tmp_path, max_consecutive_errors=3)
    with pytest.raises(BotStoppedError):
        run(runner.run_forever(asyncio.Event()))
    assert types(events).count("bot_error") == 3


def test_milestone_every_10_levels(tmp_path):
    # FakeGame: level = 1 + score // 50, i.e. a new level every 5 moves.
    runner, events, _ = make_runner(FakeGame(moves_per_life=60), {"goal": {"levels_cleared": 100}}, tmp_path)
    run(runner.run_forever(asyncio.Event(), max_runs=1))
    ms = [e.data for e in events if e.type == "milestone"]
    assert [m["cleared"] for m in ms] == [10]
    assert ms[0]["remaining"] == 90
    levels = [e.data["level"] for e in events if e.type == "level_up"]
    assert 11 not in levels and 10 in levels and 12 in levels   # the milestone replaces that level-up


def test_move_speed_tracking(tmp_path):
    runner, _, _ = make_runner(FakeGame(), {}, tmp_path)
    for _ in range(120):
        runner._record_move_time(1.0)
    for _ in range(40):
        runner._record_move_time(2.5)
    assert runner.baseline_move_s == 1.0
    assert runner.recent_move_s == 2.5
    runner.reset_recent_moves()
    assert runner.recent_move_s is None
