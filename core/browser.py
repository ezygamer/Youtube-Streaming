"""Chromium lifecycle via Playwright (async API).

Async is used because later phases run the bot, overlay updates and the
watchdog concurrently in one event loop.
"""
import logging
from dataclasses import dataclass, field

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright

from core.tap_indicator import TAP_INDICATOR_JS

log = logging.getLogger("streamer.browser")

# Requests matching these substrings are aborted so the bot never pollutes
# the game's real analytics.
DEFAULT_BLOCKED_HOSTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "connect.facebook.net",
    "posthog.com",
)


@dataclass
class BrowserOptions:
    headless: bool = False
    viewport_width: int = 450
    viewport_height: int = 800
    # < 1 shrinks the visible window (e.g. 0.45 so a 1080x1920 scene fits a
    # 1080p monitor). The page still renders at full CSS size and screenshots
    # stay full resolution; only the on-screen window is scaled.
    window_scale: float = 1.0
    # Draw a ring where the bot taps, so moves are easy to follow on screen and in videos.
    show_taps: bool = True
    blocked_hosts: tuple[str, ...] = DEFAULT_BLOCKED_HOSTS
    # Lets game sounds play without a user gesture (needed for audio capture later).
    extra_args: list[str] = field(default_factory=lambda: ["--autoplay-policy=no-user-gesture-required"])


class BrowserSession:
    def __init__(self, options: BrowserOptions | None = None):
        self.options = options or BrowserOptions()
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.blocked_requests = 0
        self.page_errors: list[str] = []

    async def start(self) -> Page:
        o = self.options
        self._pw = await async_playwright().start()
        args = list(o.extra_args)
        if not o.headless and o.window_scale != 1.0:
            args.append(f"--force-device-scale-factor={o.window_scale}")
        self.browser = await self._pw.chromium.launch(headless=o.headless, args=args)
        self.context = await self.browser.new_context(
            viewport={"width": o.viewport_width, "height": o.viewport_height},
        )
        if o.blocked_hosts:
            await self.context.route("**/*", self._filter_request)
        if o.show_taps:
            await self.context.add_init_script(TAP_INDICATOR_JS)
        self.page = await self.context.new_page()
        self.page.on("pageerror", self._on_page_error)
        self.page.on("crash", lambda _: log.error("Page crashed"))
        log.info("Chromium started (headless=%s, viewport=%dx%d)", o.headless, o.viewport_width, o.viewport_height)
        return self.page

    async def _filter_request(self, route: Route) -> None:
        url = route.request.url
        if any(host in url for host in self.options.blocked_hosts):
            self.blocked_requests += 1
            log.debug("Blocked %s", url)
            await route.abort()
        else:
            await route.continue_()

    def _on_page_error(self, exc) -> None:
        self.page_errors.append(str(exc))
        log.warning("Page error: %s", exc)

    def is_alive(self) -> bool:
        return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())

    async def stop(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer:
                    await closer.close()
            except Exception as exc:  # already dead is fine during shutdown
                log.debug("Ignoring close error: %s", exc)
        if self._pw:
            await self._pw.stop()
        self.page = self.context = self.browser = self._pw = None
        log.info("Chromium stopped")
