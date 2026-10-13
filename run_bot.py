"""EzyGamers GameStreamer - plays a web game automatically, with optional
9:16 stream scene, YouTube Live and local recordings, supervised 24/7.

Examples:
  .venv\\Scripts\\python run_bot.py                                   # watch the bot play (window)
  .venv\\Scripts\\python run_bot.py --mode marathon --stream --preview-scale 0.45
  .venv\\Scripts\\python run_bot.py --output file --headless --duration 120   # one test video
  .venv\\Scripts\\python run_bot.py --mode marathon --headless --record       # record only (no YouTube)
  .venv\\Scripts\\python run_bot.py --mode marathon --headless --live --record   # 24/7: YouTube + recordings

Stop with Ctrl+C (the current run is saved first; press twice to force quit).
"""
import argparse
import asyncio
import logging
import signal
import sys

from core.app import AppOptions, StreamApp
from core.config import ROOT, available_games, load_dotenv
from core.logging_setup import setup_logging

log = logging.getLogger("streamer")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--game", default="sumlink", help=f"one of: {', '.join(available_games())}")
    p.add_argument("--mode", help="game mode from the game's config.json, e.g. marathon, normal, high_score")
    p.add_argument("--headless", action="store_true", help="run Chromium without a window")
    p.add_argument("--stream", action="store_true", help="render the 1080x1920 stream scene with overlay")
    p.add_argument("--preview-scale", type=float, default=1.0,
                   help="shrink the visible window (e.g. 0.45) to preview the stream scene on a normal monitor")
    p.add_argument("--live", action="store_true", help="stream to YouTube (needs YOUTUBE_STREAM_KEY in .env)")
    p.add_argument("--kick", action="store_true",
                   help="stream to Kick (needs KICK_STREAM_URL and KICK_STREAM_KEY in .env); works with or without --live")
    p.add_argument("--twitch", action="store_true",
                   help="stream to Twitch (needs TWITCH_STREAM_KEY in .env); combinable with --live/--kick")
    p.add_argument("--record", action="store_true",
                   help="save overlay + clean game-only recordings in recordings/ (1-hour files)")
    p.add_argument("--output", choices=["none", "file", "youtube"], default="none",
                   help="'file' = one quick test video in recordings/; 'youtube' = same as --live")
    p.add_argument("--runs", type=int, help="stop after this many runs (default: forever)")
    p.add_argument("--duration", type=float, help="stop after this many seconds (default: forever)")
    p.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING (default: env LOG_LEVEL or INFO)")
    return p.parse_args()


def install_ctrl_c(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def handler(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt  # second Ctrl+C: quit immediately
        log.info("Stopping after the current move (Ctrl+C again to force)...")
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGINT, handler)


async def main(args) -> int:
    stop = asyncio.Event()
    install_ctrl_c(stop)
    app = StreamApp(AppOptions(
        game=args.game, mode=args.mode, headless=args.headless, preview_scale=args.preview_scale,
        stream=args.stream, live=args.live or args.output == "youtube", kick=args.kick, twitch=args.twitch, record=args.record,
        test_file=args.output == "file", runs=args.runs, duration=args.duration,
    ))
    return await app.run(stop)


if __name__ == "__main__":
    args = parse_args()
    load_dotenv()
    setup_logging(ROOT / "logs", args.log_level)
    try:
        sys.exit(asyncio.run(main(args)))
    except KeyboardInterrupt:
        log.warning("Force quit")
        sys.exit(130)
