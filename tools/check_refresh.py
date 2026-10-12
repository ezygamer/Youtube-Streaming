"""Live check of the marathon's self-repair paths.

  1. Planned refresh: with the refresh interval forced to 1 minute, the browser
     is refreshed at the start of a new level and the SAME run continues.
  2. Browser killed during the "stage cleared" moment: the run resumes and the
     game moves on to the next level (instead of sitting on an empty board).

Usage: .venv\\Scripts\\python tools\\check_refresh.py
"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.app import AppOptions, StreamApp  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from games.sumlink import rules  # noqa: E402

log = logging.getLogger("streamer.check")


async def wait_for(cond, timeout, step=0.25):
    for _ in range(int(timeout / step)):
        if cond():
            return True
        await asyncio.sleep(step)
    return cond()


async def main() -> int:
    setup_logging(ROOT / "logs")
    app = StreamApp(AppOptions(mode="marathon", headless=True, stream=True))
    app.wd_cfg["browser_refresh_minutes"] = 1
    app._session_policy.backoff_s = (2,)
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        (log.info if ok else log.error)("%s %s %s", "PASS" if ok else "FAIL", name, detail)

    stop = asyncio.Event()
    task = asyncio.create_task(app.run(stop))
    try:
        await wait_for(lambda: app.runner and app.runner.run and app.runner.run.moves > 3, 60)
        run_id = app.runner.run.run_id

        # 1. Planned refresh after ~1 minute, at a new level.
        ok = await wait_for(lambda: app.planned_refreshes >= 1 and app.session and app.session.is_alive()
                            and app.runner.bot_state == "playing", 240)
        level_at, score_at = app.runner.run.level, app.runner.run.score
        await asyncio.sleep(10)
        r = app.runner.run
        check("planned refresh happened", ok, f"refreshes={app.planned_refreshes}")
        check("refresh did not count as a failure", app._session_policy.total_restarts == 0,
              f"failure restarts={app._session_policy.total_restarts}")
        check("same run continues after refresh, still scoring",
              r.run_id == run_id and r.score > score_at and r.level >= level_at,
              f"run #{r.run_id}, stage {level_at}->{r.level}, score {score_at}->{r.score}")

        # 2. Kill the browser exactly while a level shows as cleared.
        app.wd_cfg["browser_refresh_minutes"] = 999   # no more planned refreshes
        hit = False
        for _ in range(2400):  # up to ~10 min
            try:
                raw = await app.runner.adapter._raw_state()
            except Exception:
                raw = None
            if raw and raw.get("board") and rules.is_stage_cleared(raw["board"]):
                hit = True
                break
            await asyncio.sleep(0.25)
        check("caught a 'stage cleared' moment", hit)
        if hit:
            cleared_stage = raw.get("stage")
            app.snapshot = await app.runner.adapter.snapshot()   # snapshot of the cleared board
            log.info("TEST: killing Chromium while stage %s shows as cleared", cleared_stage)
            await app.session.browser.close()
            ok = await wait_for(lambda: app._session_policy.total_restarts >= 1 and app.session
                                and app.session.is_alive() and app.runner.bot_state == "playing", 90)
            await asyncio.sleep(15)
            r = app.runner.run
            check("after the crash: same run continues on the NEXT level",
                  ok and r.run_id == run_id and r.level >= (cleared_stage or 0) + 1,
                  f"cleared stage {cleared_stage} -> now stage {r.level}, run #{r.run_id}")
            moves_then = r.moves
            await asyncio.sleep(10)
            check("bot keeps making moves (not stuck on an empty board)", app.runner.run.moves > moves_then,
                  f"moves {moves_then} -> {app.runner.run.moves}")
    finally:
        stop.set()
        code = await task
        check("clean shutdown", code == 0, f"exit code {code}")

    passed = sum(ok for _, ok, _ in results)
    print(f"\n==== Self-repair check: {passed}/{len(results)} passed ====")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
