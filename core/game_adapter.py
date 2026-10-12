"""Interface every game plugin implements.

The core streamer (bot runner, overlay, watchdog, FFmpeg) only talks to games
through this class, so adding a game means adding games/<name>/adapter.py
without touching the core.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Frame, Page

from core.game_view import DirectView


@dataclass
class GameState:
    score: int = 0
    level: int = 1
    progress: float = 0.0          # 0..1 within the current level, if the game has a notion of it
    best_score: int = 0            # the game's own best (per browser profile; not the streamer's record)
    game_over: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # game-specific details


@dataclass(frozen=True)
class StepResult:
    """What one bot step did. The runner uses `kind` for combos and flow:
      scored    - a scoring move (extends the combo)
      assist    - a non-scoring move such as Add row (breaks the combo)
      wait      - nothing to do right now (e.g. level transition animation)
      game_over - no move possible
    """
    kind: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind} {self.detail}".strip()


class GameAdapter(ABC):
    def __init__(self, page: Page, base_url: str, config: dict, mode: dict | None = None, view=None):
        self.page = page
        self.base_url = base_url
        self.config = config
        self.mode = mode or {}
        # The view decides which frame the game lives in (whole page, or the
        # stream overlay's iframe). Adapters must use self.frame, not self.page,
        # for anything inside the game.
        self.view = view or DirectView()
        self.frame: Frame | None = None
        # Set by the app when game sounds are streamed: sound(name) plays one of
        # the game's own sound effects (see config "sounds"). None = silent.
        self.sound = None

    def play_sound(self, name: str) -> None:
        if self.sound:
            try:
                self.sound(name)
            except Exception:
                pass  # sound must never break gameplay

    @abstractmethod
    async def open(self) -> None:
        """Load the game and get it to a playable screen."""

    @abstractmethod
    async def read_state(self) -> GameState:
        """Return the current state without changing the game."""

    @abstractmethod
    async def is_game_over(self) -> bool:
        ...

    @abstractmethod
    async def restart(self) -> None:
        """Start a completely fresh run (score 0)."""

    @abstractmethod
    async def step(self) -> StepResult:
        """Make one move."""

    async def snapshot(self) -> dict | None:
        """Everything needed to put the game back exactly where it is, e.g.
        after a browser crash. None if the game can't be snapshotted."""
        return None

    async def restore(self, snapshot: dict) -> None:
        """Load a snapshot taken by snapshot() into the (freshly opened) game."""

    async def continue_after_game_over(self) -> bool:
        """Keep the current run going after game over, if the game allows it
        (used by Endless mode). Returns False if unsupported."""
        return False
