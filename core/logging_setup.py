"""Logging configuration shared by every component.

Log files (in logs/ by default):
  streamer.log - everything (system, browser, server, game)
  game.log     - gameplay events only (logger name "game" and its children)
  errors.log   - WARNING and above from any component
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5


def _file_handler(path: Path, level: int) -> logging.Handler:
    handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    return handler


def _make_console_safe() -> None:
    # Windows consoles often use cp1252, which can't print emoji used in
    # stream messages; replace unprintable characters instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(log_dir: str | Path = "logs", level: str | None = None) -> None:
    _make_console_safe()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    level_value = getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_value)
    # Allow calling setup_logging() more than once (e.g. in tests) without duplicating output.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(level_value)
    console.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(console)
    root.addHandler(_file_handler(log_dir / "streamer.log", level_value))
    root.addHandler(_file_handler(log_dir / "errors.log", logging.WARNING))

    game_logger = logging.getLogger("game")
    for h in list(game_logger.handlers):
        game_logger.removeHandler(h)
    game_logger.addHandler(_file_handler(log_dir / "game.log", level_value))
