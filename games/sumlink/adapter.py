"""SumLink adapter: drives the unmodified SumLink web build from outside.

How it works (see README "SumLink analysis"):
- State: the game persists its whole state as JSON in localStorage["gameState"]
  on every change, so we read that rather than scraping the DOM.
- Controls: real DOM clicks on grid cells (#main-game-grid > div, index i is
  row i//9, col i%9) and the #add-row-btn button, exactly like a player.
- Game over: the 'Out of Moves!' popup, cross-checked with the game's own rule
  (no valid pair and no add-rows left).
- Fresh restart: delete localStorage["gameState"] and reload; the game then
  builds stage 1 by itself. (window.resetGame() is avoided: it also wipes bestScore.)
"""
import asyncio
import json
import logging
import time

from core.game_adapter import GameAdapter, GameState, StepResult
from games.sumlink import rules
from games.sumlink.strategies import make_strategy

log = logging.getLogger("game.sumlink")

GRID_CELLS = "#main-game-grid > div"
ADD_ROW_BUTTON = "#add-row-btn"

# Seeded before the app's scripts run on every load so the bot skips the
# language picker and tutorial. currentScreen is JSON-encoded because the app
# reads it through its useLocalStorage hook; the other two are read raw.
_SEED_STORAGE_JS = """
(() => {
  try {
    localStorage.setItem('gameLanguage', 'en');
    localStorage.setItem('tutorialCompleted', 'true');
    localStorage.setItem('currentScreen', '"game"');
  } catch (e) {}
})();
"""


class GameNotReadyError(RuntimeError):
    pass


class SumLinkAdapter(GameAdapter):
    def __init__(self, page, base_url, config, mode=None, view=None):
        super().__init__(page, base_url, config, mode, view)
        # Mode-specific timing (e.g. speed_run) overrides the game defaults.
        t = {**config.get("timing", {}), **self.mode.get("timing", {})}
        self.strategy = make_strategy(self.mode.get("strategy", "first_pair"))
        self.try_again_text = config.get("try_again_text", "Try Again")
        self.click_delay = t.get("click_delay_ms", 250) / 1000
        self.settle_timeout = t.get("settle_timeout_ms", 5000) / 1000
        self.settle_quiet = t.get("settle_quiet_ms", 300) / 1000
        self.load_timeout_ms = t.get("load_timeout_ms", 15000)
        self.game_over_text = config.get("game_over_text", "Out of Moves!")
        self.url = (config.get("url") or base_url).rstrip("/") + config.get("language_path", "/english")
        self._seeded = False
        self._low_match_voice_played = False

    # ---------- loading ----------

    async def open(self) -> None:
        """(Re)load everything: the view (overlay, if any) and then the game.
        Also used to recover from a frozen or broken page."""
        if not self._seeded:
            # Page-level init scripts also run inside iframes.
            await self.page.add_init_script(_SEED_STORAGE_JS)
            self._seeded = True
        self.frame = await self.view.prepare(self.page)
        await self._load_game()

    async def _load_game(self) -> None:
        log.info("Opening %s", self.url)
        await self.frame.goto(self.url, wait_until="domcontentloaded", timeout=self.load_timeout_ms)
        await self._wait_until_playable()
        await self._fix_cleared_board()

    async def _fix_cleared_board(self) -> None:
        """A page (re)loaded during the 'stage cleared' animation finds a board
        with no numbers left. The game only moves on to the next stage from its
        win animation, which a reload skips - so it would sit on an empty board
        forever. Save 'next stage, no board' instead: on load the game then
        builds the next stage itself, keeping the score (its own code path)."""
        raw = await self._raw_state() or {}
        board = raw.get("board") or []
        if not board or not rules.is_stage_cleared(board):
            return
        nxt = {k: v for k, v in raw.items() if k not in ("board", "initialBoard", "flashRows")}
        nxt["stage"] = raw.get("stage", 1) + 1
        log.warning("Loaded a cleared board (stage %s) - continuing with stage %s", raw.get("stage"), nxt["stage"])
        await self.frame.evaluate("s => localStorage.setItem('gameState', s)", json.dumps(nxt))
        await self.frame.goto(self.url, wait_until="domcontentloaded", timeout=self.load_timeout_ms)
        await self._wait_until_playable()

    async def _wait_until_playable(self) -> None:
        await self.frame.wait_for_selector(GRID_CELLS, timeout=self.load_timeout_ms)
        deadline = time.monotonic() + self.load_timeout_ms / 1000
        while time.monotonic() < deadline:
            raw = await self._raw_state()
            if raw and raw.get("board"):
                await self.wait_until_settled()
                return
            await asyncio.sleep(0.1)
        raise GameNotReadyError("Game grid appeared but no game state was saved")

    # ---------- state ----------

    async def _raw_state_text(self) -> str | None:
        return await self.frame.evaluate("() => localStorage.getItem('gameState')")

    async def _raw_state(self) -> dict | None:
        text = await self._raw_state_text()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Could not parse gameState JSON")
            return None

    async def wait_until_settled(self) -> dict | None:
        """Wait until the saved state stops changing.

        A match goes through several intermediate saves over ~0.7s
        (highlight -> flash removed rows -> compacted board + new score),
        so reading too early gives a stale board.
        """
        deadline = time.monotonic() + self.settle_timeout
        last_text, stable_since = None, time.monotonic()
        while True:
            text = await self._raw_state_text()
            now = time.monotonic()
            if text != last_text:
                last_text, stable_since = text, now
            state = json.loads(text) if text else None
            busy = bool(state) and (
                bool(state.get("flashRows"))
                or any(c and c.get("tempSelected") for c in state.get("board") or [])
            )
            if not busy and now - stable_since >= self.settle_quiet:
                return state
            if now >= deadline:
                log.warning("State did not settle within %.1fs", self.settle_timeout)
                return state
            await asyncio.sleep(0.05)

    async def read_state(self) -> GameState:
        raw = await self._raw_state() or {}
        board = raw.get("board") or []
        best_text = await self.frame.evaluate("() => localStorage.getItem('bestScore')")
        active = len(rules.active_indices(board))
        initial_active = len(rules.active_indices(raw.get("initialBoard") or [])) or active or 1
        add_rows_left = raw.get("addRowsLeft", 0)
        return GameState(
            score=raw.get("score", 0),
            level=raw.get("stage", 1),
            progress=max(0.0, min(1.0, 1 - active / initial_active)),
            best_score=int(json.loads(best_text)) if best_text else 0,
            game_over=bool(board) and active > 0 and rules.is_out_of_moves(board, add_rows_left),
            extra={
                "active_cells": active,
                "valid_moves": len(rules.find_valid_pairs(board)),
                "add_rows_left": add_rows_left,
                "hints_left": raw.get("hintsLeft", 0),
                "stage_cleared": bool(board) and rules.is_stage_cleared(board),
            },
        )

    async def game_over_popup_visible(self) -> bool:
        return await self.frame.get_by_text(self.game_over_text, exact=True).is_visible()

    async def is_game_over(self) -> bool:
        if await self.game_over_popup_visible():
            return True
        return (await self.read_state()).game_over

    # ---------- actions ----------

    async def click_cell(self, index: int) -> None:
        await self.frame.locator(GRID_CELLS).nth(index).click(timeout=5000)

    async def _wait_for(self, predicate, what: str) -> bool:
        """Poll saved state until predicate(state) is true, then let it settle."""
        deadline = time.monotonic() + self.settle_timeout
        while time.monotonic() < deadline:
            state = await self._raw_state()
            if state and predicate(state):
                await self.wait_until_settled()
                return True
            await asyncio.sleep(0.05)
        log.warning("Timed out waiting for %s", what)
        return False

    async def make_match(self, i1: int, i2: int) -> bool:
        # Waiting for the score is required, not just for the state to go quiet:
        # mid-match the game briefly re-saves the *pre-match* board (its storage
        # hook applies updates to a stale copy) and only writes the new score
        # ~400ms later, so a "quiet" state can still be the old one.
        before = (await self._raw_state() or {}).get("score", 0)
        await self.click_cell(i1)
        await asyncio.sleep(self.click_delay)
        await self.click_cell(i2)
        self.play_sound("match")   # the game plays it on the second tap of a valid pair
        ok = await self._wait_for(lambda s: s.get("score", 0) > before, f"score to rise after match {i1}+{i2}")
        if ok:
            await self._after_move_sounds()
        return ok

    async def add_row(self) -> bool:
        before = (await self._raw_state() or {}).get("addRowsLeft", 0)
        await self.frame.locator(ADD_ROW_BUTTON).click(timeout=5000)
        self.play_sound("click")
        ok = await self._wait_for(lambda s: s.get("addRowsLeft", 0) < before, "add row")
        if ok:
            await self._after_move_sounds()
        return ok

    async def _after_move_sounds(self) -> None:
        """Sounds the game plays in reaction to the new board (GameBoard.js):
        the bell when the stage is cleared, and the voice prompt when exactly
        2 matches are left on stage 3 or later (once, until matches go up)."""
        if not self.sound:
            return
        raw = await self._raw_state() or {}
        board = raw.get("board") or []
        if not board:
            return
        if rules.is_stage_cleared(board):
            self.play_sound("win")
            return
        pairs = len(rules.find_valid_pairs(board))
        if pairs == 2 and raw.get("stage", 1) > 2 and not self._low_match_voice_played:
            self._low_match_voice_played = True
            self.play_sound("low_matches")
        elif pairs > 2:
            self._low_match_voice_played = False

    async def restart(self) -> None:
        log.info("Restarting with a fresh run")
        await self.frame.evaluate("() => localStorage.removeItem('gameState')")
        await self._load_game()

    async def snapshot(self) -> dict | None:
        # The game keeps its whole state in localStorage, so a copy of those
        # keys is a complete snapshot.
        try:
            snap = await self.frame.evaluate(
                "() => ({gameState: localStorage.getItem('gameState'), bestScore: localStorage.getItem('bestScore')})")
        except Exception:
            return None
        return snap if snap and snap.get("gameState") else None

    async def restore(self, snapshot: dict) -> None:
        await self.frame.evaluate(
            """s => { localStorage.setItem('gameState', s.gameState);
                      if (s.bestScore) localStorage.setItem('bestScore', s.bestScore); }""", snapshot)
        await self._load_game()
        log.info("Restored game snapshot")

    async def continue_after_game_over(self) -> bool:
        """Endless mode: press the game's own 'Try Again', which replays the
        current stage while keeping the accumulated score (stage >= 2)."""
        button = self.frame.get_by_role("button", name=self.try_again_text, exact=True)
        try:
            # The game opens its popup ~0.5s after running out of moves.
            await button.wait_for(state="visible", timeout=5000)
        except Exception:
            log.warning("'%s' button did not appear", self.try_again_text)
            return False
        before = (await self._raw_state() or {}).get("addRowsLeft", 0)
        await button.click(timeout=5000)
        # Try Again rebuilds the stage with 5 fresh add-rows.
        return await self._wait_for(lambda s: s.get("addRowsLeft", 0) > before, "Try Again")

    async def step(self) -> StepResult:
        """One deterministic move chosen by the mode's strategy."""
        raw = await self.wait_until_settled() or {}
        board = raw.get("board") or []
        if not board:
            raise GameNotReadyError("No board in game state")
        if rules.is_stage_cleared(board):
            # The game advances to the next stage by itself after ~2.5s.
            await asyncio.sleep(0.5)
            return StepResult("wait", "next stage")
        # The strategy is CPU-bound (look-ahead can take tens of ms); run it off
        # the event loop so other tasks (overlay, watchdog) stay responsive.
        decision = await asyncio.to_thread(self.strategy.choose, board, raw.get("addRowsLeft", 0))
        if decision.kind == "match":
            ok = await self.make_match(*decision.pair)
            if not ok:
                raise GameNotReadyError(f"Match {decision.pair} was not accepted by the game")
            return StepResult("scored", f"match {decision.pair[0]}+{decision.pair[1]}")
        if decision.kind == "add_row":
            if not await self.add_row():
                raise GameNotReadyError("Add row was not accepted by the game")
            return StepResult("assist", "add row")
        return StepResult("game_over")
