"""Restart policies: retry with backoff, but never a rapid restart loop.

Each component (browser session, YouTube output, recorder) has its own
policy. Restarts back off (5s, 15s, 30s, 60s, 120s); if a component needs
more than `max_restarts` restarts within `window_s`, the next wait is the
long `cooldown_s`. A component that has run fine for `stable_after_s` gets
its counters reset.
"""
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("streamer.watchdog")


@dataclass
class RestartPolicy:
    name: str
    backoff_s: tuple[float, ...] = (5, 15, 30, 60, 120)
    max_restarts: int = 5
    window_s: float = 600
    cooldown_s: float = 300
    stable_after_s: float = 600
    _attempts: list[float] = field(default_factory=list)
    _started_at: float = 0.0
    total_restarts: int = 0

    def mark_started(self) -> None:
        self._started_at = time.monotonic()

    def next_delay(self) -> float:
        """Record a restart attempt and return how long to wait before it."""
        now = time.monotonic()
        if self._started_at and now - self._started_at >= self.stable_after_s:
            self._attempts.clear()  # it had been running fine; start counting afresh
        self._attempts = [t for t in self._attempts if now - t < self.window_s]
        self._attempts.append(now)
        self.total_restarts += 1
        if len(self._attempts) > self.max_restarts:
            log.error("%s restarted %d times in %.0f min - cooling down for %.0f s",
                      self.name, len(self._attempts) - 1, self.window_s / 60, self.cooldown_s)
            self._attempts.clear()
            return self.cooldown_s
        return self.backoff_s[min(len(self._attempts) - 1, len(self.backoff_s) - 1)]
