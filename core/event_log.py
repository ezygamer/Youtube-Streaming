"""Turns bot events into readable lines in logs/game.log."""
import logging

from core.events import Event

log = logging.getLogger("game")


def _fmt_seconds(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def log_event(e: Event) -> None:
    d = e.data
    t = e.type
    if t == "game_started":
        extra = f", challenge: {d['challenge']}" if d.get("challenge") else ""
        log.info("Game started (run #%s, mode %s, record %s%s)", d.get("run"), d.get("mode"), d.get("high_score"), extra)
    elif t == "level_up":
        log.info("Stage %s reached (score %s)", d["level"], d["score"])
    elif t == "milestone":
        left = f", {d['remaining']} to go" if d.get("remaining") else ""
        log.info("MILESTONE: %s levels cleared (score %s%s)", d["cleared"], d["score"], left)
    elif t == "combo":
        log.info("Combo x%s!", d["combo"])
    elif t == "new_high_score":
        log.info("New high score: %s (previous %s)", d["score"], d["previous"])
    elif t == "game_over":
        if d.get("continuing"):
            log.info("Game over at stage %s, score %s - continuing (endless)", d["level"], d["score"])
        else:
            log.info("Game over: score %s, stage %s, %s moves, max combo x%s, %s",
                     d["score"], d["level"], d.get("moves"), d.get("max_combo"), _fmt_seconds(d.get("seconds")))
    elif t == "challenge_completed":
        log.info("Challenge completed: %s in %s (score %s)", d["challenge"], _fmt_seconds(d["seconds"]), d["score"])
    elif t == "challenge_failed":
        log.info("Challenge failed: %s (score %s, stage %s)", d["challenge"], d["score"], d["level"])
    elif t == "goal_reached":
        rec = " NEW RECORD TIME!" if d.get("record") else f" (best {_fmt_seconds(d.get('previous_best'))})"
        log.info("GOAL REACHED (%s): stage %s, score %s in %s.%s",
                 d["goal"], d.get("level"), d.get("score"), _fmt_seconds(d["seconds"]), rec)
    elif t == "run_ended":
        log.info("Run ended (%s): score %s, stage %s, %s", d["reason"], d["score"], d["level"], _fmt_seconds(d["seconds"]))
    elif t == "bot_error":
        log.warning("Bot error (attempt %s): %s", d.get("attempt"), d.get("error"))
    else:
        log.info("%s %s", t, d)
