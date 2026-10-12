"""Phase 5 check: break things on purpose and verify the watchdog recovers.

  1. Kill Chromium mid-run      -> browser restarts, the SAME run resumes (same score/stage)
  2. Kill the recorder FFmpeg   -> recorder restarts
  3. Kill the YouTube FFmpeg    -> stream reconnects (simulated: RTMP replaced by a local FLV file)

Usage: .venv\\Scripts\\python tools\\check_recovery.py
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.app import AppOptions, StreamApp  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402

log = logging.getLogger("streamer.check")
OUT = ROOT / "probe_output"


async def wait_for(cond, timeout, step=0.5):
    for _ in range(int(timeout / step)):
        if cond():
            return True
        await asyncio.sleep(step)
    return cond()


async def main() -> int:
    setup_logging(ROOT / "logs")
    # Simulated YouTube: the "RTMP server" is a local FLV file.
    os.environ["YOUTUBE_RTMP_URL"] = str(OUT)
    os.environ["YOUTUBE_STREAM_KEY"] = "SIMULATED-KEY-recovery.flv"
    app = StreamApp(AppOptions(mode="marathon", headless=True, live=True, record=True))
    # Faster restarts for the test.
    app._session_policy.backoff_s = (2,)
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        (log.info if ok else log.error)("%s %s %s", "PASS" if ok else "FAIL", name, detail)

    stop = asyncio.Event()
    task = asyncio.create_task(app.run(stop))
    try:
        ok = await wait_for(lambda: app.runner and app.runner.run and app.runner.run.score >= 100
                            and all(o.ffmpeg for o in app.outputs), 120)
        check("started: bot playing, youtube + recorder running", ok,
              f"outputs={[o.name for o in app.outputs if o.ffmpeg]}")
        for o in app.outputs:
            o.policy.backoff_s = (2,)

        # 1. Browser crash
        await asyncio.sleep(6)  # let a fresh snapshot be taken
        run_id, score, level = app.runner.run.run_id, app.runner.run.score, app.runner.run.level
        log.info("TEST: killing Chromium (run #%s, stage %s, score %s)", run_id, level, score)
        await app.session.browser.close()
        ok = await wait_for(lambda: app._session_policy.total_restarts >= 1 and app.session is not None
                            and app.session.is_alive() and app.runner.bot_state == "playing", 90)
        await asyncio.sleep(8)
        r = app.runner.run
        check("browser crash -> browser restarted", ok, f"restarts={app._session_policy.total_restarts}")
        check("browser crash -> same run resumed (not restarted at level 1)",
              r.run_id == run_id and r.score >= score and r.level >= level,
              f"before: run #{run_id} stage {level} score {score}; after: run #{r.run_id} stage {r.level} score {r.score}")

        # 2. Recorder crash
        rec = next(o for o in app.outputs if o.name == "recorder")
        rec.ffmpeg.proc.kill()
        ok = await wait_for(lambda: rec.policy.total_restarts >= 1 and rec.ffmpeg and rec.ffmpeg.is_healthy(), 60)
        check("recorder FFmpeg killed -> restarted", ok, f"restarts={rec.policy.total_restarts}")

        # 3. YouTube drop
        yt = next(o for o in app.outputs if o.name == "youtube")
        yt.ffmpeg.proc.kill()
        ok = await wait_for(lambda: yt.policy.total_restarts >= 1 and yt.ffmpeg and yt.ffmpeg.is_healthy(), 60)
        await asyncio.sleep(5)
        check("YouTube FFmpeg killed -> reconnected", ok and yt.ffmpeg.frames_written > 30,
              f"restarts={yt.policy.total_restarts}, frames since reconnect={yt.ffmpeg.frames_written if yt.ffmpeg else 0}")

        log_text = (ROOT / "logs" / "streamer.log").read_text(encoding="utf-8", errors="replace")
        check("stream key never logged", "SIMULATED-KEY" not in log_text)
        check("bot still playing at the end", app.runner.bot_state == "playing", app.runner.bot_state)
    finally:
        stop.set()
        code = await task
        check("clean shutdown", code == 0, f"exit code {code}")
        for f in OUT.glob("SIMULATED-KEY*"):
            f.unlink()

    passed = sum(ok for _, ok, _ in results)
    print(f"\n==== Recovery check: {passed}/{len(results)} passed ====")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
