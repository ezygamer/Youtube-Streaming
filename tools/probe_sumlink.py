"""Phase 1 probe: verifies on the real game that SumLink can be automated.

Checks, in order:
  1. Game loads over local HTTP straight into gameplay (no language/tutorial screens)
  2. Score / stage / board / add-rows can be read from saved state
  3. Fresh restart gives stage 1, score 0
  4. Clicking a valid pair scores +10 (and the on-screen score agrees)
  5. A short deterministic play-through advances stages
  6. Game Over is detected (popup + rule) on an injected dead-end board
  7. Restart from Game Over gives a fresh stage 1
  8. No JS page errors (analytics requests, if the build has any, are blocked)

Usage:
  .venv\\Scripts\\python tools\\probe_sumlink.py            # visible browser
  .venv\\Scripts\\python tools\\probe_sumlink.py --headless
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.browser import BrowserOptions, BrowserSession  # noqa: E402
from core.game_server import GameServer  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from games.sumlink.adapter import SumLinkAdapter  # noqa: E402

log = logging.getLogger("streamer.probe")
OUT = ROOT / "probe_output"

# A board with two active, unmatchable numbers and no add-rows left:
# the game itself should declare "Out of Moves!".
DEAD_END_STATE = {
    "board": [{"value": 1, "state": "active"}, {"value": 2, "state": "blurred"}, {"value": 3, "state": "active"}]
    + [{"value": None, "state": "cleared"}] * 105,
    "initialBoard": [],
    "stage": 3,
    "score": 120,
    "hintsLeft": 0,
    "addRowsLeft": 0,
}


class Probe:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, ok, detail))
        (log.info if ok else log.error)("%s %s %s", "PASS" if ok else "FAIL", name, detail)
        return ok


async def screen_score(frame) -> str:
    # The first score card's number is the live score shown to the player.
    return (await frame.locator(".score-card p.font-bold").first.inner_text()).strip()


async def run(headless: bool, play_steps: int) -> int:
    setup_logging(ROOT / "logs")
    OUT.mkdir(exist_ok=True)
    config = json.loads((ROOT / "games/sumlink/config.json").read_text())
    probe = Probe()

    server = GameServer(ROOT / config["build_dir"]).start()
    session = BrowserSession(BrowserOptions(
        headless=headless,
        viewport_width=config["viewport"]["width"],
        viewport_height=config["viewport"]["height"],
    ))
    try:
        page = await session.start()
        game = SumLinkAdapter(page, server.base_url, config)

        # 1. Load
        await game.open()
        on_language_screen = await game.frame.get_by_text("Choose Language", exact=False).count() > 0
        url = game.frame.url
        probe.check("load: game grid shown at /english", "/english" in url and not on_language_screen, url)
        await page.screenshot(path=OUT / "1_loaded.png")

        # 2 + 3. Fresh restart and state reading
        await game.restart()
        s = await game.read_state()
        probe.check("restart: fresh run is stage 1, score 0", s.level == 1 and s.score == 0,
                    f"stage={s.level} score={s.score}")
        probe.check("state: board readable", s.extra["active_cells"] > 0,
                    f"active={s.extra['active_cells']} valid_moves={s.extra['valid_moves']} "
                    f"add_rows={s.extra['add_rows_left']} hints={s.extra['hints_left']} best={s.best_score}")

        # 4. One match
        before = s.score
        action = await game.step()
        s = await game.read_state()
        shown = await screen_score(game.frame)
        probe.check("control: clicking a valid pair scores +10", s.score == before + 10 and shown == str(s.score),
                    f"{action}: saved score {before}->{s.score}, on-screen '{shown}'")
        await page.screenshot(path=OUT / "2_after_first_match.png")

        # 5. Short play-through
        max_stage, actions = s.level, {"scored": 0, "assist": 0, "wait": 0, "game_over": 0}
        for _ in range(play_steps):
            action = await game.step()
            actions[action.kind] += 1
            s = await game.read_state()
            max_stage = max(max_stage, s.level)
            if action.kind == "game_over":
                break
        probe.check("play: bot advances through stages", max_stage >= 2,
                    f"reached stage {max_stage}, score {s.score}, actions {actions}")
        await page.screenshot(path=OUT / "3_after_play.png")

        # 6. Game over detection on an injected dead-end board
        await game.frame.evaluate("s => localStorage.setItem('gameState', JSON.stringify(s))", DEAD_END_STATE)
        await game.frame.goto(game.url, wait_until="domcontentloaded")
        await game.frame.wait_for_selector("#main-game-grid > div")
        popup = False
        for _ in range(30):  # the game shows the popup ~0.5s after load
            if await game.game_over_popup_visible():
                popup = True
                break
            await asyncio.sleep(0.1)
        s = await game.read_state()
        probe.check("game over: 'Out of Moves!' popup detected", popup)
        probe.check("game over: rule-based detection agrees", s.game_over and await game.is_game_over(),
                    f"valid_moves={s.extra['valid_moves']} add_rows={s.extra['add_rows_left']}")
        await page.screenshot(path=OUT / "4_game_over.png")

        # 7. Restart from game over
        await game.restart()
        s = await game.read_state()
        probe.check("restart: from game over gives fresh stage 1",
                    s.level == 1 and s.score == 0 and not await game.game_over_popup_visible(),
                    f"stage={s.level} score={s.score}")
        await page.screenshot(path=OUT / "5_restarted.png")

        # 8. Hygiene
        # Informational: some builds include analytics (blocked), others don't.
        log.info("Analytics requests blocked: %d", session.blocked_requests)
        probe.check("no JS page errors", not session.page_errors, "; ".join(session.page_errors[:3]))
    except Exception:
        log.exception("Probe crashed")
        probe.check("probe completed without exceptions", False)
    finally:
        await session.stop()
        server.stop()

    passed = sum(ok for _, ok, _ in probe.results)
    print(f"\n==== Phase 1 probe: {passed}/{len(probe.results)} checks passed ====")
    for name, ok, detail in probe.results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"Screenshots: {OUT}")
    return 0 if passed == len(probe.results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headless", action="store_true", help="run Chromium without a window")
    parser.add_argument("--play-steps", type=int, default=60, help="moves for the play-through check")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.headless, args.play_steps)))
