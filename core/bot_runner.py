"""Game-agnostic bot loop: plays runs forever according to a mode.

Responsibilities:
- start runs, step the adapter, detect game over, restart or continue
- track combo / level / high score / mode goals and emit events
- keep a `status` snapshot for the overlay and dashboard (later phases)
- recover from step errors with limited retries and backoff

Browser/FFmpeg crash recovery is the watchdog's job (Phase 5); this loop
only handles problems inside the game page.
"""
import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

from core.events import EventBus
from core.game_adapter import GameAdapter, GameState
from core.stats_db import StatsDB

log = logging.getLogger("streamer.bot")


@dataclass
class RunnerSettings:
    restart_delay_s: float = 3.0         # pause between runs so viewers see the end
    score_log_interval_s: float = 30.0
    combo_milestones: tuple[int, ...] = (5, 10, 20, 30, 50, 75, 100)
    max_consecutive_errors: int = 5       # then give up and let the caller/watchdog decide
    error_backoff_s: tuple[float, ...] = (1, 2, 5, 10, 30)
    stall_steps: int = 40                 # this many steps without any score/level change = frozen


@dataclass
class RunStats:
    run_id: int
    started: float = field(default_factory=time.monotonic)
    score: int = 0
    level: int = 1
    max_level: int = 1
    moves: int = 0
    combo: int = 0
    max_combo: int = 0
    progress: float = 0.0
    beat_record: bool = False
    finished: bool = False


class BotStoppedError(RuntimeError):
    pass


def goal_met(goal: dict, score: int, level: int) -> bool:
    """Goal keys (all given keys must be met):
      score: N           score >= N
      level: N           reached level N
      levels_cleared: N  finished level N (i.e. now on level N+1)
    """
    return (("score" not in goal or score >= goal["score"])
            and ("level" not in goal or level >= goal["level"])
            and ("levels_cleared" not in goal or level > goal["levels_cleared"]))


def goal_level(goal: dict) -> int | None:
    """The level number to show as 'x / N' on the overlay, if any."""
    return goal.get("levels_cleared") or goal.get("level")


class BotRunner:
    def __init__(self, adapter: GameAdapter, game_id: str, game_name: str, mode_name: str, mode: dict,
                 bus: EventBus, db: StatsDB, settings: RunnerSettings | None = None):
        self.adapter = adapter
        self.game_id, self.game_name = game_id, game_name
        self.mode_name, self.mode = mode_name, mode
        self.bus, self.db = bus, db
        self.settings = settings or RunnerSettings()
        self.run: RunStats | None = None
        self.runs_completed = 0
        self.high_score = db.high_score(game_id, mode_name)
        self._challenge_index = 0
        # What the overlay shows: the current challenge, or the one just
        # finished (with its result) until the next run starts.
        self._shown_challenge: dict | None = None
        self._last_score_log = 0.0
        self.bot_state = "starting"
        # Watchdog support: time of the last completed bot step, and a flag
        # that makes cancellation "suspend" the run instead of ending it, so
        # it can be resumed in a new browser (e.g. after a Chromium crash).
        self.last_step_at = time.monotonic()
        self.suspending = False
        # Move-speed tracking for the watchdog's slowdown detection.
        self._move_times: deque[float] = deque(maxlen=40)
        self._baseline_moves: list[float] = []
        self.level_started_at = 0.0       # when the current level's board appeared

    # ---------- public ----------

    @property
    def challenge(self) -> dict | None:
        challenges = self.mode.get("challenges") or []
        return challenges[self._challenge_index % len(challenges)] if challenges else None

    @property
    def goal(self) -> dict:
        return (self.challenge or {}).get("goal") or self.mode.get("goal") or {}

    def status(self) -> dict:
        """Snapshot for overlay/dashboard."""
        r = self.run
        return {
            "game": self.game_name,
            "mode": self.mode.get("label", self.mode_name),
            "bot_state": self.bot_state,
            "run_id": r.run_id if r else None,
            "score": r.score if r else 0,
            "level": r.level if r else 1,
            "progress": round(r.progress, 3) if r else 0.0,
            "combo": r.combo if r else 0,
            "max_combo": r.max_combo if r else 0,
            "high_score": max(self.high_score, r.score if r else 0),
            "record_beaten": bool(r and r.beat_record),
            "run_seconds": round(time.monotonic() - r.started) if r else 0,
            "challenge": self._shown_challenge,
            "goal_level": goal_level(self.goal),
            "runs_completed": self.runs_completed,
        }

    @property
    def has_active_run(self) -> bool:
        return self.run is not None and not self.run.finished

    async def run_forever(self, stop: asyncio.Event, max_runs: int | None = None, resume: bool = False) -> None:
        """Play runs until stopped. With resume=True and an unfinished run
        (suspended by the watchdog), continue that run in the already-restored
        game instead of starting a fresh one."""
        self.suspending = False
        self.last_step_at = time.monotonic()
        if not (resume and self.has_active_run):
            await self.adapter.open()
        while not stop.is_set():
            if resume and self.has_active_run:
                resume = False
                log.info("Resuming run #%d at stage %d, score %d", self.run.run_id, self.run.level, self.run.score)
                self.bot_state = "playing"
                reason = await self._run_body(stop)
            else:
                reason = await self._play_one_run(stop)
            self.runs_completed += 1
            if reason == "stopped" or (max_runs and self.runs_completed >= max_runs):
                break
            self.bot_state = "celebrating" if reason in ("goal_reached", "challenge_completed") else "restarting"
            # Longer pause after a big moment so viewers see the celebration.
            delay = (self.mode.get("pause_after_goal_s", self.settings.restart_delay_s)
                     if self.bot_state == "celebrating" else self.settings.restart_delay_s)
            log.info("Restarting game in %.0fs", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ---------- one run ----------

    async def _play_one_run(self, stop: asyncio.Event) -> str:
        await self.adapter.restart()
        self.run = RunStats(run_id=self.db.start_run(self.game_id, self.mode_name))
        self.bot_state = "playing"
        ch = self.challenge
        self._shown_challenge = ({"name": ch["name"], "goal": ch.get("goal"),
                                  "time_limit_s": ch.get("time_limit_s"), "state": "in_progress"}
                                 if ch else None)
        state = await self.adapter.read_state()
        self._absorb_state(state)
        self._emit("game_started", run=self.runs_completed + 1, high_score=self.high_score,
                   challenge=(self.challenge or {}).get("name"))
        return await self._run_body(stop)

    async def _run_body(self, stop: asyncio.Event) -> str:
        reason = "stopped"
        try:
            reason = await self._loop(stop)
        except BotStoppedError:
            # Too many errors in this browser: keep the run open so the
            # watchdog can resume it in a fresh browser.
            self.suspending = True
            raise
        finally:
            if not self.suspending:
                self._finish_run(reason)
        return reason

    async def _loop(self, stop: asyncio.Event) -> str:
        errors = 0
        steps_without_progress = 0
        r = self.run
        while not stop.is_set():
            # Always give other tasks (overlay, watchdog, Ctrl+C) a turn, even if
            # an adapter's step happens to complete without suspending.
            await asyncio.sleep(0)
            try:
                t0 = time.monotonic()
                result = await self.adapter.step()
                state = await self.adapter.read_state()
                errors = 0
                self.last_step_at = time.monotonic()
                if result.kind in ("scored", "assist"):
                    self._record_move_time(self.last_step_at - t0)
            except Exception as exc:
                errors += 1
                await self._recover_from_error(exc, errors)
                continue

            r.moves += result.kind in ("scored", "assist")
            if result.kind == "scored":
                r.combo += 1
                r.max_combo = max(r.max_combo, r.combo)
                if r.combo in self.settings.combo_milestones:
                    self._emit("combo", combo=r.combo, score=state.score)
            elif result.kind == "assist":
                r.combo = 0

            progressed = self._absorb_state(state)
            steps_without_progress = 0 if (progressed or result.kind == "wait") else steps_without_progress + 1
            if steps_without_progress >= self.settings.stall_steps:
                log.warning("No progress for %d steps - reloading game", steps_without_progress)
                steps_without_progress = 0
                await self.adapter.open()
                continue

            self._maybe_log_score()

            outcome = self._check_goal()
            if outcome:
                return outcome

            if result.kind == "game_over" or state.game_over:
                if not await self.adapter.is_game_over():
                    continue  # transient - state said over but the game disagrees
                if self.mode.get("on_game_over") == "continue":
                    self._emit("game_over", score=r.score, level=r.level, continuing=True)
                    if await self.adapter.continue_after_game_over():
                        r.combo = 0
                        log.info("Continuing run with Try Again (score kept: %d)", r.score)
                        continue
                    log.warning("Could not continue after game over - starting a new run")
                return "game_over"
        return "stopped"

    async def _recover_from_error(self, exc: Exception, errors: int) -> None:
        s = self.settings
        log.error("Bot step failed (%d/%d): %s", errors, s.max_consecutive_errors, exc, exc_info=errors == 1)
        self._emit("bot_error", error=str(exc), attempt=errors)
        if errors >= s.max_consecutive_errors:
            self.bot_state = "error"
            raise BotStoppedError(f"Giving up after {errors} consecutive errors: {exc}") from exc
        delay = s.error_backoff_s[min(errors - 1, len(s.error_backoff_s) - 1)]
        self.bot_state = "recovering"
        await asyncio.sleep(delay)
        try:
            # Reloading keeps the run: the game restores itself from localStorage.
            await self.adapter.open()
            self.bot_state = "playing"
        except Exception as reload_exc:
            log.error("Reload failed: %s", reload_exc)

    # ---------- move speed ----------

    def _record_move_time(self, seconds: float) -> None:
        self._move_times.append(seconds)
        # The baseline is this process's normal speed: the median of its first
        # 100 moves (skipping the first 20, while everything warms up).
        if len(self._baseline_moves) < 120:
            self._baseline_moves.append(seconds)

    @property
    def baseline_move_s(self) -> float | None:
        return statistics.median(self._baseline_moves[20:]) if len(self._baseline_moves) >= 120 else None

    @property
    def recent_move_s(self) -> float | None:
        return statistics.median(self._move_times) if len(self._move_times) >= 30 else None

    def reset_recent_moves(self) -> None:
        """After a browser refresh: judge the new browser on its own moves."""
        self._move_times.clear()

    # ---------- state tracking ----------

    def _absorb_state(self, state: GameState) -> bool:
        """Update run stats from the game; emit level/high-score events.
        Returns True if score or level changed."""
        r = self.run
        changed = state.score != r.score or state.level != r.level
        if state.level != r.level:
            self.level_started_at = time.monotonic()
        if state.level > r.max_level:
            r.max_level = state.level
            cleared_goal = self.goal.get("levels_cleared")
            cleared = state.level - 1
            if cleared_goal and state.level > cleared_goal:
                pass  # the goal itself is the celebration
            elif cleared >= 10 and cleared % 10 == 0:
                # Every 10 levels: a bigger moment than a plain level-up.
                self._emit("milestone", cleared=cleared, level=state.level, score=state.score,
                           remaining=(cleared_goal - cleared) if cleared_goal else None)
            else:
                self._emit("level_up", level=state.level, score=state.score)
        r.score, r.level, r.progress = state.score, state.level, state.progress
        # Only celebrate beating an existing record, once per run.
        if self.high_score > 0 and not r.beat_record and r.score > self.high_score:
            r.beat_record = True
            self._emit("new_high_score", score=r.score, previous=self.high_score)
        return changed

    def _check_goal(self) -> str | None:
        r, goal = self.run, self.goal
        if not goal:
            return None
        elapsed = time.monotonic() - r.started
        if goal_met(goal, r.score, r.level):
            if self.challenge:
                self._emit("challenge_completed", challenge=self.challenge["name"], seconds=round(elapsed), score=r.score)
                self._shown_challenge["state"] = "completed"
                self._challenge_index += 1
                return "challenge_completed"
            best = self.db.best_time(self.game_id, self.mode_name, "goal_reached")
            self._emit("goal_reached", seconds=round(elapsed, 1), goal=goal, score=r.score, level=r.level,
                       goal_level=goal_level(goal), record=best is None or elapsed < best, previous_best=best)
            return "goal_reached"
        limit = (self.challenge or {}).get("time_limit_s")
        if limit and elapsed > limit:
            self._emit("challenge_failed", challenge=self.challenge["name"], score=r.score, level=r.level)
            self._shown_challenge["state"] = "failed"
            self._challenge_index += 1
            return "challenge_failed"
        return None

    def _maybe_log_score(self) -> None:
        now = time.monotonic()
        if now - self._last_score_log >= self.settings.score_log_interval_s:
            self._last_score_log = now
            r = self.run
            logging.getLogger("game").info("Score: %d (stage %d, combo x%d, %d moves)", r.score, r.level, r.combo, r.moves)

    def _finish_run(self, reason: str) -> None:
        r = self.run
        r.finished = True
        duration = time.monotonic() - r.started
        self.db.end_run(r.run_id, score=r.score, max_level=r.max_level, moves=r.moves,
                        max_combo=r.max_combo, end_reason=reason, duration_s=duration)
        if reason == "game_over":
            self._emit("game_over", score=r.score, level=r.max_level, moves=r.moves,
                       max_combo=r.max_combo, seconds=round(duration), continuing=False)
        else:
            self._emit("run_ended", reason=reason, score=r.score, level=r.max_level, seconds=round(duration))
        if r.score > self.high_score:
            self.high_score = r.score

    def _emit(self, type_: str, **data) -> None:
        event = self.bus.emit(type_, self.game_name, mode=self.mode_name, **data)
        self.db.record_event(event, self.run.run_id if self.run else None)
