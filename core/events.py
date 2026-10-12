"""Minimal in-process event bus.

The bot runner emits events (game_started, new_high_score, level_up, combo,
game_over, ...). Subscribers - the logger, the stats DB, and later the
overlay and clip recorder - react to them without the runner knowing about them.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger("streamer.events")


@dataclass
class Event:
    type: str
    game: str
    data: dict[str, Any] = field(default_factory=dict)
    time: datetime = field(default_factory=datetime.now)


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self):
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def emit(self, type_: str, game: str, **data) -> Event:
        event = Event(type_, game, data)
        for fn in self._subscribers:
            try:
                fn(event)
            except Exception:
                # One broken subscriber must never stop the game loop.
                log.exception("Event subscriber %r failed on %s", fn, type_)
        return event
