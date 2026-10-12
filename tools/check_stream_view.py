"""Phase 3 check: renders the 1080x1920 stream scene and verifies it.

Checks:
  1. Page is exactly 1080x1920 and the game iframe is fully inside it
  2. The bot can play inside the scaled iframe (score goes up)
  3. Overlay numbers match the bot's status (score, stage, combo, record)
  4. Event messages appear on screen
  5. Challenge panel shows in challenge mode
Screenshots go to probe_output/stream_*.png.

Usage: .venv\\Scripts\\python tools\\check_stream_view.py [--seconds 60] [--mode challenge]
"""
import argparse
import asyncio
import logging
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

log = logging.getLogger("streamer.check")
OUT = ROOT / "probe_output"

READ_OVERLAY_JS = """() => ({
  score: document.getElementById('score').dataset.value,
  level: document.getElementById('level').textContent,
  combo: document.getElementById('combo').textContent,
  record: document.getElementById('record').textContent,
  mode: document.getElementById('mode').textContent,
  challengeHidden: document.getElementById('challenge').hidden,
  challenge: document.getElementById('challenge-name').textContent,
  toasts: [...document.querySelectorAll('.toast')].map(t => t.textContent),
  page: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
  frame: (() => { const r = document.getElementById('frame-wrap').getBoundingClientRect();
                  return [r.left, r.top, r.right, r.bottom]; })(),
  // Lowest bottom edge of the panels above the game, and top of the footer.
  above: Math.max(...['stats', 'infobar', 'challenge'].map(id => {
           const el = document.getElementById(id);
           return el.hidden ? 0 : el.getBoundingClientRect().bottom; })),
  below: document.getElementById('bottom').getBoundingClientRect().top,
})"""


async def run(seconds: float, mode_arg: str | None) -> int:
    setup_logging(ROOT / "logs")
    OUT.mkdir(exist_ok=True)
    config = load_game_config("sumlink")
    mode_name, mode = resolve_mode(config, mode_arg)
    stream_cfg = load_stream_config()
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        (log.info if ok else log.error)("%s %s %s", "PASS" if ok else "FAIL", name, detail)

    bus = EventBus()
    bus.subscribe(log_event)
    seen_events = []
    bus.subscribe(lambda e: seen_events.append(e.type))
    db = StatsDB(OUT / "check_stats.db")  # separate DB: don't pollute real records
    server = GameServer(config["build_dir"], mounts={"/__stream__": ROOT / "stream" / "overlay"}).start()
    gv = config["stream"]["game_viewport"]
    view = StreamView(f"{server.base_url}/__stream__/index.html?w={gv['width']}&h={gv['height']}")
    session = BrowserSession(BrowserOptions(headless=True, viewport_width=1080, viewport_height=1920))
    stop = asyncio.Event()
    try:
        page = await session.start()
        adapter = load_adapter_class(config)(page, server.base_url, config, mode, view)
        runner = BotRunner(adapter, "sumlink", config["name"], mode_name, mode, bus, db,
                           RunnerSettings(restart_delay_s=1))
        overlay = OverlayController(page, runner, bus, stream_cfg["overlay"])
        overlay.start()
        bot = asyncio.create_task(runner.run_forever(stop))

        # Let the game load and the bot make a few moves.
        await asyncio.sleep(8)
        await page.screenshot(path=OUT / "stream_1_start.png")
        o = await page.evaluate(READ_OVERLAY_JS)
        check("scene is 1080x1920", o["page"] == [1080, 1920], f"page={o['page']}")
        l, t, r, b = o["frame"]
        check("game fully visible inside scene", l >= 0 and t >= 0 and r <= 1080 and b <= 1920,
              f"game box=({l:.0f},{t:.0f})-({r:.0f},{b:.0f}) = {r - l:.0f}x{b - t:.0f}")
        check("game does not overlap header/footer panels", t >= o["above"] and b <= o["below"],
              f"panels end at y={o['above']:.0f}, game y={t:.0f}..{b:.0f}, footer starts y={o['below']:.0f}")
        if o["toasts"]:
            check("event message shown", True, f"{o['toasts']}")

        score_before = runner.status()["score"]
        await asyncio.sleep(seconds)
        status = runner.status()
        await overlay.push()
        await asyncio.sleep(0.3)
        o = await page.evaluate(READ_OVERLAY_JS)
        await page.screenshot(path=OUT / "stream_2_playing.png")
        check("bot plays inside scaled iframe", status["score"] > score_before,
              f"score {score_before} -> {status['score']}, stage {status['level']}")
        check("overlay score/stage/combo match bot", (o["score"], o["level"], o["combo"]) ==
              (str(status["score"]), str(status["level"]), f"x{status['combo']}"),
              f"overlay={o['score']}/{o['level']}/{o['combo']}")
        check("overlay record matches", o["record"] == f"{status['high_score']:,}", f"record={o['record']}")
        check("mode shown", o["mode"] == f"{status['mode'].upper()} MODE", o["mode"])
        if mode.get("challenges"):
            check("challenge panel visible", not o["challengeHidden"] and status["challenge"]["name"] in o["challenge"],
                  o["challenge"])

        # Force a message to verify the toast layer renders above the game
        # without blocking the bot's clicks.
        bus.emit("new_high_score", config["name"], mode=mode_name, score=status["score"], previous=0)
        await asyncio.sleep(1.2)
        o = await page.evaluate(READ_OVERLAY_JS)
        await page.screenshot(path=OUT / "stream_3_message.png")
        check("event message renders", any("HIGH SCORE" in t for t in o["toasts"]), f"{o['toasts']}")
        score_mid = runner.status()["score"]
        await asyncio.sleep(4)
        check("bot keeps playing under a message", runner.status()["score"] > score_mid,
              f"{score_mid} -> {runner.status()['score']}")
        check("events reached overlay", "game_started" in seen_events, f"{sorted(set(seen_events))}")
        check("no JS page errors", not session.page_errors, "; ".join(session.page_errors[:3]))

        stop.set()
        await asyncio.wait_for(bot, timeout=30)
        await overlay.stop()
    except Exception:
        log.exception("Check crashed")
        check("completed without exceptions", False)
    finally:
        await session.stop()
        server.stop()
        db.close()

    passed = sum(ok for _, ok, _ in results)
    print(f"\n==== Phase 3 stream view: {passed}/{len(results)} checks passed ====")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"Screenshots: {OUT}\\stream_*.png")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=40)
    p.add_argument("--mode", default="challenge")
    a = p.parse_args()
    sys.exit(asyncio.run(run(a.seconds, a.mode)))
