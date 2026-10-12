"""Where the game is rendered inside the browser page.

DirectView: the game is the whole page (bot only, probes).
StreamView: the page is the 1080x1920 stream overlay and the game runs in its
            <iframe id="game">. Same origin, so the bot can drive it normally.

Adapters only ever use the Frame returned by prepare(), so they work the same
in both views.
"""
import logging

from playwright.async_api import Frame, Page

log = logging.getLogger("streamer.view")


class DirectView:
    async def prepare(self, page: Page) -> Frame:
        return page.main_frame


class StreamView:
    def __init__(self, overlay_url: str, load_timeout_ms: int = 15000):
        self.overlay_url = overlay_url
        self.load_timeout_ms = load_timeout_ms

    async def prepare(self, page: Page) -> Frame:
        log.info("Loading stream overlay %s", self.overlay_url)
        await page.goto(self.overlay_url, wait_until="domcontentloaded", timeout=self.load_timeout_ms)
        iframe = await page.wait_for_selector("iframe#game", state="attached", timeout=self.load_timeout_ms)
        frame = await iframe.content_frame()
        if frame is None:
            raise RuntimeError("Overlay has no game iframe")
        return frame
