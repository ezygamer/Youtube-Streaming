"""Live check of the Level-100 marathon ending (no need to play 2 hours).

Starts the marathon mode in the stream scene, then jumps the game to stage
100 with a real random board. The bot must clear it, the game must advance to
101, and the runner must celebrate (goal_reached), then restart at level 1.

Usage: .venv\\Scripts\\python tools\\check_level100.py [--stage 100]
"""
import argparse
import asyncio
import json
import logging
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bot_runner import BotRunner, RunnerSettings  # noqa: E402
from core.browser import BrowserOptions, BrowserSession  # noqa: E402
from core.config import load_adapter_class, load_game_config, load_stream_config, resolve_mode  # noqa: E402
from core.event_log import log_event  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.game_server import GameServer  # noqa: E402
from core.game_view import StreamView  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from core.overlay import OverlayController  # noqa: E402
from core.stats_db import StatsDB  # noqa: E402
from games.sumlink import rules  # noqa: E402

log = logging.getLogger("streamer.check")
OUT = ROOT / "probe_output"


async def run(stage: int) -> int:
    setup_logging(ROOT / "logs")
    config = load_game_config("sumlink")
    mode_name, mode = resolve_mode(config, "marathon")
    goal = mode["goal"]["levels_cleared"]
    bus = EventBus()
    bus.subscribe(log_event)
    events = []
    bus.subscribe(events.append)
    db = StatsDB(OUT / "check_stats.db")
    server = GameServer(config["build_dir"], mounts={"/__stream__": ROOT / "stream" / "overlay"}).start()
    gv = config["stream"]["game_viewport"]
    view = StreamView(f"{server.base_url}/__stream__/index.html?w={gv['width']}&h={gv['height']}")
    session = BrowserSession(BrowserOptions(headless=True, viewport_width=1080, viewport_height=1920))
    stop = asyncio.Event()
    results = []
    try:
        page = await session.start()
        adapter = load_adapter_class(config)(page, server.base_url, config, mode, view)
        runner = BotRunner(adapter, "sumlink", config["name"], mode_name, {**mode, "pause_after_goal_s": 8},
                           bus, db, RunnerSettings(restart_delay_s=1))
        overlay = OverlayController(page, runner, bus, load_stream_config()["overlay"])
        overlay.start()
        bot = asyncio.create_task(runner.run_forever(stop))
        while not any(e.type == "game_started" for e in events):
            await asyncio.sleep(0.2)
        await asyncio.sleep(2)

        # Jump to the target stage with a real board, as the game itself would build it.
        board = rules.generate_board(stage, random.Random(stage))
        state = {"board": board, "initialBoard": board, "stage": stage, "score": 24_750,
                 "hintsLeft": 5, "addRowsLeft": 5}
        await adapter.frame.evaluate("s => localStorage.setItem('gameState', s)", json.dumps(state))
        await adapter.frame.goto(adapter.url)
        log.info("Jumped to stage %d", stage)
        await asyncio.sleep(6)
        await page.screenshot(path=OUT / "level100_1_playing.png")

        # Wait for the celebration (stage cleared -> next level -> goal).
        for _ in range(600):
            if any(e.type == "goal_reached" for e in events):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.5)
        await page.screenshot(path=OUT / "level100_2_celebration.png")
        goal_events = [e for e in events if e.type == "goal_reached"]
        results.append(("goal reached after clearing stage %d" % goal, bool(goal_events),
                        goal_events[0].data if goal_events else "no goal event"))

        # Then a fresh run must start at level 1.
        starts_before = sum(e.type == "game_started" for e in events)
        for _ in range(60):
            if sum(e.type == "game_started" for e in events) > starts_before:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)
        st = runner.status()
        results.append(("restarted at level 1 after celebration", st["level"] <= 2 and st["run_id"] is not None,
                        f"level={st['level']} score={st['score']}"))
        await page.screenshot(path=OUT / "level100_3_restarted.png")
        results.append(("no JS page errors", not session.page_errors, "; ".join(session.page_errors[:2])))
        stop.set()
        await asyncio.wait_for(bot, 30)
        await overlay.stop()
    finally:
        await session.stop()
        server.stop()
        db.close()

    passed = sum(ok for _, ok, _ in results)
    print(f"\n==== Level {goal} check: {passed}/{len(results)} passed ====")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--stage", type=int, default=100)
    sys.exit(asyncio.run(run(p.parse_args().stage)))
