"""Grabs rendered frames from a Chromium page via the DevTools screencast.

Chromium only sends a frame when the page repaints, so the capture keeps the
latest frame and the encoder pump re-sends it at a constant frame rate.
Works in headless mode (no window or screen capture needed).
"""
import asyncio
import base64
import logging
import time

from playwright.async_api import CDPSession, Page

log = logging.getLogger("streamer.capture")


class ScreencastCapture:
    def __init__(self, page: Page, width: int, height: int, quality: int = 85, every_nth_frame: int = 2):
        self.page = page
        # Chromium repaints at up to 60/s while anything animates; we only send
        # 30 fps, so every 2nd repaint is enough and halves the JPEG work.
        self.every_nth_frame = every_nth_frame
        self.width, self.height = width, height
        self.quality = quality
        self._cdp: CDPSession | None = None
        self.latest: bytes | None = None      # latest JPEG frame
        self.frames_received = 0
        self.last_frame_at = 0.0               # monotonic time of last frame from Chromium

    async def start(self) -> None:
        self._cdp = await self.page.context.new_cdp_session(self.page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": self.quality,
            "maxWidth": self.width,
            "maxHeight": self.height,
            "everyNthFrame": self.every_nth_frame,
        })
        log.info("Screencast started (%dx%d, jpeg q=%d)", self.width, self.height, self.quality)

    def _on_frame(self, params: dict) -> None:
        self.latest = base64.b64decode(params["data"])
        self.frames_received += 1
        self.last_frame_at = time.monotonic()
        # Chromium stops sending frames until each one is acknowledged.
        asyncio.ensure_future(self._ack(params["sessionId"]))

    async def _ack(self, session_id: int) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception as exc:  # page closing/reloading
            log.debug("Frame ack failed: %s", exc)

    def seconds_since_last_frame(self) -> float:
        return time.monotonic() - self.last_frame_at if self.last_frame_at else float("inf")

    async def stop(self) -> None:
        if self._cdp:
            try:
                await self._cdp.send("Page.stopScreencast")
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
