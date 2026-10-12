"""SQLite store for runs, high scores and notable events.

The game's own "Best" lives in the browser profile and resets whenever
Chromium starts fresh, so the streamer keeps its own records here.
The events table is the hook for the later clip/highlight system.
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from core.events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game        TEXT NOT NULL,
    mode        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    score       INTEGER DEFAULT 0,
    max_level   INTEGER DEFAULT 1,
    moves       INTEGER DEFAULT 0,
    max_combo   INTEGER DEFAULT 0,
    end_reason  TEXT,
    duration_s  REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_game_mode ON runs(game, mode, score);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    game    TEXT NOT NULL,
    type    TEXT NOT NULL,
    run_id  INTEGER,
    data    TEXT
);
"""

# Only events worth keeping long-term (and later clipping) are stored.
STORED_EVENTS = {
    "game_started", "game_over", "new_high_score", "level_up", "combo",
    "challenge_completed", "challenge_failed", "goal_reached", "milestone", "bot_error",
}


class StatsDB:
    def __init__(self, path: str | Path = "data/stats.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def start_run(self, game: str, mode: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (game, mode, started_at) VALUES (?, ?, ?)",
                (game, mode, datetime.now().isoformat(timespec="seconds")),
            )
            return cur.lastrowid

    def end_run(self, run_id: int, *, score: int, max_level: int, moves: int,
                max_combo: int, end_reason: str, duration_s: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET ended_at=?, score=?, max_level=?, moves=?, max_combo=?, end_reason=?, duration_s=? "
                "WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), score, max_level, moves,
                 max_combo, end_reason, round(duration_s, 1), run_id),
            )

    def high_score(self, game: str, mode: str | None = None) -> int:
        sql, args = "SELECT MAX(score) FROM runs WHERE game=?", [game]
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return row[0] or 0

    def best_time(self, game: str, mode: str, end_reason: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(duration_s) FROM runs WHERE game=? AND mode=? AND end_reason=?",
                (game, mode, end_reason),
            ).fetchone()
        return row[0]

    def record_event(self, event: Event, run_id: int | None = None) -> None:
        if event.type not in STORED_EVENTS:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (time, game, type, run_id, data) VALUES (?, ?, ?, ?, ?)",
                (event.time.isoformat(timespec="seconds"), event.game, event.type, run_id,
                 json.dumps(event.data, default=str)),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
