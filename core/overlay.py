"""Keeps the stream overlay page in sync with the bot.

- Pushes runner.status() to window.ezOverlay.update() every update interval.
- Turns bot events into temporary on-screen messages, using the templates in
  config/stream.json ("messages"). Templates can use {score}, {level}, {combo},
  {time}, {challenge}, {mode} and can differ per mode:
      "game_started": {"speed_run": "⚡ SPEED MODE!", "default": "CAN THE BOT BEAT THE RECORD?"}
  An empty template disables that message.

Failures here are logged and ignored: the overlay must never stop the bot.
"""
import asyncio
import logging
from string import Formatter

from playwright.async_api import Page

from core.bot_runner import BotRunner
from core.events import Event, EventBus

log = logging.getLogger("streamer.overlay")

_PUSH_JS = """
([cfg, status, messages]) => {
  const o = window.ezOverlay;
  if (!o) return false;
  if (!o.configured) o.configure(cfg);
  o.update(status);
  for (const m of messages) o.message(m.text, m.kind, m.ms, m.urgent);
  return true;
}
"""


def _clock(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


class OverlayController:
    def __init__(self, page: Page, runner: BotRunner, bus: EventBus, settings: dict):
        self.page = page
        self.runner = runner
        self.settings = settings
        self.interval = settings.get("update_interval_ms", 500) / 1000
        self.messages_cfg = settings.get("messages", {})
        self.message_ms = settings.get("message_ms", 2200)
        # Big moments stay on screen longer.
        self.celebration_ms = settings.get("celebration_ms", 6000)
        self.page_config = {k: settings[k] for k in ("title", "tagline", "cta") if k in settings}
        self._pending: list[dict] = []
        self._task: asyncio.Task | None = None
        self.pushes = 0
        bus.subscribe(self._on_event)

    # ---------- events -> messages ----------

    def _template_for(self, event: Event) -> tuple[str, str] | None:
        cfg = self.messages_cfg.get(event.type)
        if cfg is None:
            return None
        if isinstance(cfg, str):
            cfg = {"text": cfg}
        # Per-mode override: {"speed_run": "...", "default": "..."} or {"text": ..., "kind": ...}
        mode = event.data.get("mode")
        chosen = cfg.get(mode, cfg.get("default", cfg))
        if isinstance(chosen, str):
            chosen = {"text": chosen}
        text = chosen.get("text") or cfg.get("text")
        kind = chosen.get("kind") or cfg.get("kind", "")
        return (text, kind) if text else None

    def _on_event(self, event: Event) -> None:
        # Endless-mode game overs are not the end of the run; skip the
        # "game over" banner unless a specific template exists for it.
        if event.type == "game_over" and event.data.get("continuing"):
            event = Event("game_over_continue", event.game, event.data, event.time)
        tpl = self._template_for(event)
        if not tpl:
            return
        text, kind = tpl
        values = _SafeDict(event.data)
        values["time"] = _clock(event.data.get("seconds"))
        values["score"] = f"{event.data.get('score', 0):,}" if "score" in event.data else ""
        try:
            text = Formatter().vformat(text, (), values)
        except (ValueError, IndexError) as exc:
            log.warning("Bad message template for %s: %s", event.type, exc)
            return
        big = event.type in ("goal_reached", "challenge_completed")
        self._pending.append({"text": text, "kind": kind, "urgent": big,
                              "ms": self.celebration_ms if big else self.message_ms})

    # ---------- push loop ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="overlay")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def push(self) -> bool:
        messages, self._pending = self._pending, []
        try:
            ok = await self.page.evaluate(_PUSH_JS, [self.page_config, self.runner.status(), messages])
        except Exception as exc:  # page reloading/navigating - try again next tick
            log.debug("Overlay push skipped: %s", exc)
            ok = False
        if not ok:
            # Keep messages for the next try, but only a few: stale banners are useless.
            self._pending = (messages + self._pending)[-3:]
        else:
            self.pushes += 1
        return bool(ok)

    async def _loop(self) -> None:
        while True:
            await self.push()
            await asyncio.sleep(self.interval)
