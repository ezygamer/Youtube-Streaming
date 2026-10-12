"""Browser page -> screencast -> FFmpeg (constant frame rate).

The screencast keeps the latest frame; FFmpeg's writer thread re-sends it at
a fixed rate. If the page does not repaint, the previous frame is repeated,
so the output always has the steady frame rate YouTube requires.
"""
import asyncio
import logging
import time
from pathlib import Path

from playwright.async_api import Page

from core.capture import ScreencastCapture
from core.ffmpeg_output import FfmpegOutput, OutputTarget

log = logging.getLogger("streamer.pipeline")


class StreamPipeline:
    def __init__(self, page: Page, ffmpeg: str, target: OutputTarget, width: int, height: int,
                 cfg: dict, root: Path):
        self.capture = ScreencastCapture(page, width, height, cfg.get("jpeg_quality", 85))
        self.output = FfmpegOutput(ffmpeg, target, width, height, cfg, root)
        self.target = target
        self.stats_interval = cfg.get("stats_log_interval_s", 30)
        self._stats_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.capture.start()
        self.output.start(lambda: self.capture.latest)
        self._stats_task = asyncio.create_task(self._log_stats(), name="stream-stats")

    async def _log_stats(self) -> None:
        # Rates are measured per interval: FFmpeg's own fps/speed are averages
        # since process start (including the page-load wait), which understate them.
        prev_sent, prev_browser, prev_t = 0, 0, time.monotonic()
        while True:
            await asyncio.sleep(self.stats_interval)
            s = self.output.stats()
            now = time.monotonic()
            dt = now - prev_t
            send_fps = (s["written"] - prev_sent) / dt
            browser_fps = (self.capture.frames_received - prev_browser) / dt
            prev_sent, prev_browser, prev_t = s["written"], self.capture.frames_received, now
            level = log.info if send_fps >= 0.9 * self.output.fps else log.warning
            level("Stream: sending %.1f fps (target %d), browser repaints %.1f/s, %s, %d late frames total",
                  send_fps, self.output.fps, browser_fps, s["bitrate"], s["late"])

    def is_healthy(self) -> bool:
        return self.output.is_healthy()

    async def stop(self) -> None:
        if self._stats_task:
            self._stats_task.cancel()
            await asyncio.gather(self._stats_task, return_exceptions=True)
        await self.capture.stop()
        await asyncio.to_thread(self.output.stop)
