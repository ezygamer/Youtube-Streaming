"""The streamer application: bot + browser + overlay + outputs, supervised 24/7.

Watchdog checks (every 2 s):
  - bot task failed / browser crashed / no frames from browser / bot hung
        -> restart the browser session and RESUME the run from a snapshot
  - YouTube FFmpeg exited or stalled (network drop)  -> reconnect
  - recorder FFmpeg exited                           -> restart (new file)
  - browser running for many hours                   -> planned restart between runs (memory)
  - every 10 min: delete oldest recordings if over the size limit
Every restart uses a RestartPolicy (backoff + cooldown), so a persistent
problem never turns into a rapid restart loop.

While the browser restarts, the outputs keep sending the last frame, so the
YouTube stream stays connected.
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.audio_mixer import AudioMixer, fetch_sounds
from core.bot_runner import BotRunner
from core.browser import BrowserOptions, BrowserSession
from core.capture import ScreencastCapture
from core.config import ROOT, load_adapter_class, load_game_config, load_stream_config, resolve_mode
from core.event_log import log_event
from core.events import EventBus
from core.ffmpeg_output import FfmpegOutput, OutputTarget, file_target, find_ffmpeg, youtube_target
from core.game_server import GameServer
from core.game_view import DirectView, StreamView
from core.keep_awake import keep_awake
from core.overlay import OverlayController
from core.recordings import RecordingJanitor, crop_moved, game_crop, recording_targets
from core.stats_db import StatsDB
from core.watchdog import RestartPolicy

log = logging.getLogger("streamer.app")


@dataclass
class AppOptions:
    game: str = "sumlink"
    mode: str | None = None
    headless: bool = False
    preview_scale: float = 1.0
    stream: bool = False          # render the 1080x1920 overlay scene
    live: bool = False            # YouTube
    record: bool = False          # overlay + clean recordings (segmented)
    test_file: bool = False       # one MP4 file (quick test)
    runs: int | None = None
    duration: float | None = None


class _Output:
    """One supervised FFmpeg process."""

    def __init__(self, name: str, make_targets: Callable[[], list[OutputTarget] | None], policy: RestartPolicy):
        self.name = name
        self.make_targets = make_targets
        self.policy = policy
        self.ffmpeg: FfmpegOutput | None = None
        self.retry_at = 0.0


class StreamApp:
    def __init__(self, opts: AppOptions):
        self.opts = opts
        if opts.live or opts.record or opts.test_file:
            opts.stream = True
        self.config = load_game_config(opts.game)
        self.mode_name, self.mode = resolve_mode(self.config, opts.mode)
        self.stream_cfg = load_stream_config()
        self.out_cfg = self.stream_cfg["output"]
        w = self.config["watchdog"] if "watchdog" in self.config else {}
        self.wd_cfg = {**self.stream_cfg.get("watchdog", {}), **w}

        self.bus = EventBus()
        self.bus.subscribe(log_event)
        self.db: StatsDB | None = None
        self.server: GameServer | None = None
        self.session: BrowserSession | None = None
        self.capture: ScreencastCapture | None = None
        self.runner: BotRunner | None = None
        self.runner_task: asyncio.Task | None = None
        self.overlay: OverlayController | None = None
        self.outputs: list[_Output] = []
        self.mixer: AudioMixer | None = None
        self.snapshot: dict | None = None
        self._held_frame: bytes | None = None
        self._session_started_at = 0.0
        self._session_retry_at: float | None = None
        self._session_policy = RestartPolicy("Browser session")
        self.planned_refreshes = 0
        self._slow_logged = False
        self._stop: asyncio.Event | None = None
        self.started_at = time.monotonic()

    # ---------- setup ----------

    def _check_outputs(self) -> str | None:
        """Validate output requirements before starting anything. Returns an error."""
        if not (self.opts.live or self.opts.record or self.opts.test_file):
            return None
        self.ffmpeg = find_ffmpeg()
        if not self.ffmpeg:
            return "FFmpeg not found. Install it (winget install Gyan.FFmpeg) or set FFMPEG_PATH in .env"
        if self.opts.live:
            try:
                t = youtube_target()
            except RuntimeError as exc:
                return str(exc)
            # Only the last 4 characters: enough to match it against YouTube Studio's
            # key list, useless to anyone reading the log.
            log.info("YouTube stream key in use ends in ...%s", t.secret[-4:])
        if self.config.get("url"):
            return "Stream output needs the game served locally (set 'url' to null in the game config)"
        return None

    def _build_outputs(self) -> None:
        o = self.opts
        width, height = self.stream_cfg["width"], self.stream_cfg["height"]

        def make(name, targets_fn):
            policy = RestartPolicy(name, backoff_s=(5, 15, 30, 60, 120), max_restarts=8, window_s=900, cooldown_s=300)
            self.outputs.append(_Output(name, targets_fn, policy))

        if o.live:
            make("youtube", lambda: [youtube_target()])
        if o.record:
            async_crop = {"value": None}
            self._crop_holder = async_crop
            make("recorder", lambda: recording_targets(self.out_cfg, ROOT, async_crop["value"]) or None)
        if o.test_file:
            make("file", lambda: [file_target(ROOT / self.out_cfg.get("recordings", {}).get("dir", "recordings"))])
        self._out_size = (width, height)

    def _setup_audio(self) -> None:
        """Game sounds (+ optional music) for every output. If anything fails,
        the outputs fall back to the old behaviour (silence / music file)."""
        a = self.out_cfg.get("audio", {})
        game_sounds = a.get("game_sounds", True) and self.config.get("sounds")
        music = a.get("music_file")
        music_path = (ROOT / music) if music else None
        try:
            files = fetch_sounds(self.config["sounds"], ROOT / "assets" / "sfx" / self.config["id"]) if game_sounds else {}
            self.mixer = AudioMixer(self.ffmpeg, files, a.get("sound_volumes", {}), a.get("game_volume", 1.0),
                                    music_path, a.get("music_volume", 0.35))
            self.mixer.start()
        except Exception as exc:
            log.warning("Audio mixer unavailable (%s) - streaming without game sounds", exc)
            self.mixer = None

    def _frame_source(self) -> bytes | None:
        # During a browser restart, keep sending the last frame so YouTube stays connected.
        if self.capture and self.capture.latest is not None:
            return self.capture.latest
        return self._held_frame

    # ---------- browser session ----------

    async def _start_session(self, stop: asyncio.Event) -> None:
        o = self.opts
        if o.stream:
            gv = self.config.get("stream", {}).get("game_viewport", self.config["viewport"])
            view = StreamView(f"{self.server.base_url}/__stream__/index.html?w={gv['width']}&h={gv['height']}")
            width, height = self.stream_cfg["width"], self.stream_cfg["height"]
        else:
            view = DirectView()
            width, height = self.config["viewport"]["width"], self.config["viewport"]["height"]

        self.session = BrowserSession(BrowserOptions(headless=o.headless, viewport_width=width,
                                                     viewport_height=height, window_scale=o.preview_scale))
        page = await self.session.start()
        adapter = load_adapter_class(self.config)(page, self.server.base_url, self.config, self.mode, view)
        if self.mixer:
            adapter.sound = self.mixer.play   # the bot triggers the game's own sounds

        resume = bool(self.runner and self.runner.has_active_run and self.snapshot)
        if self.runner is None:
            self.runner = BotRunner(adapter, self.config["id"], self.config["name"], self.mode_name, self.mode,
                                    self.bus, self.db)
        else:
            self.runner.adapter = adapter

        if o.stream:
            if self.overlay is None:
                self.overlay = OverlayController(page, self.runner, self.bus, self.stream_cfg.get("overlay", {}))
                self.overlay.start()
            else:
                self.overlay.page = page
        if self.outputs:
            self.capture = ScreencastCapture(page, width, height, self.out_cfg.get("jpeg_quality", 85),
                                             self.out_cfg.get("capture_every_nth_frame", 2))
            await self.capture.start()

        if resume:
            await adapter.open()
            await adapter.restore(self.snapshot)
        self.runner_task = asyncio.create_task(self.runner.run_forever(stop, self.opts.runs, resume=resume),
                                               name="bot")
        self._session_started_at = time.monotonic()
        self._session_policy.mark_started()
        self._session_retry_at = None

    async def _stop_session(self) -> None:
        if self.runner_task and not self.runner_task.done():
            if self.runner and self.runner.has_active_run:
                self.runner.suspending = True   # keep the run open for resume
            self.runner_task.cancel()
            await asyncio.gather(self.runner_task, return_exceptions=True)
        if self.capture:
            self._held_frame = self.capture.latest or self._held_frame
            await self.capture.stop()
            self.capture = None
        if self.session:
            await self.session.stop()
            self.session = None

    async def _restart_session(self, reason: str) -> None:
        delay = self._session_policy.next_delay()
        log.error("Restarting browser session in %.0fs: %s", delay, reason)
        if self.runner:
            self.runner.bot_state = "recovering"
        await self._stop_session()
        self._session_retry_at = time.monotonic() + delay

    # ---------- planned refresh (slowdown self-repair) ----------

    def _refresh_reason(self, now: float, refresh_s: float, slow_factor: float) -> str | None:
        """Why the browser should be refreshed now, or None. Only answers at a
        safe moment: a fresh level board (or between runs), so the refresh is
        hardly noticeable and the snapshot holds a clean, full level."""
        r = self.runner
        if not r or not self.session:
            return None
        age = now - self._session_started_at
        base, recent = r.baseline_move_s, r.recent_move_s
        slow = bool(base and recent and recent > base * slow_factor and recent - base > 0.3)
        if slow and not self._slow_logged:
            self._slow_logged = True
            log.warning("Moves slowed down: %.2f s/move now vs %.2f s/move normally - "
                        "refreshing the browser at the next new level", recent, base)
        if not (slow or age > refresh_s):
            return None
        between_runs = r.bot_state in ("restarting", "celebrating") and not r.has_active_run
        fresh_level = (r.bot_state == "playing" and r.level_started_at > self._session_started_at
                       and now - r.level_started_at < 20)
        overdue = age > refresh_s + 1800  # no safe moment for 30 min: refresh anyway
        if not (between_runs or fresh_level or overdue):
            return None
        return (f"moves slowed to {recent:.2f}s (normal {base:.2f}s)" if slow
                else f"scheduled refresh after {age / 60:.0f} min")

    async def _planned_refresh(self, reason: str) -> None:
        """A deliberate restart: no failure backoff, and the run resumes from a
        snapshot taken right now."""
        log.info("Refreshing the browser (%s); the run continues where it is", reason)
        if self.runner and self.runner.has_active_run:
            try:
                snap = await asyncio.wait_for(self.runner.adapter.snapshot(), 5)
                if snap:
                    self.snapshot = snap
            except Exception:
                pass
        self.planned_refreshes += 1
        self._slow_logged = False
        await self._stop_session()
        if self.runner:
            self.runner.reset_recent_moves()
        self._session_retry_at = time.monotonic() + 1

    # ---------- outputs ----------

    async def _supervise_outputs(self) -> None:
        now = time.monotonic()
        for out in self.outputs:
            f = out.ffmpeg
            if f is not None:
                stalled = f.is_running() and f.last_progress_at and now - f.last_progress_at > 30
                if f.is_healthy() and not stalled:
                    continue
                why = "stalled (no progress for 30s)" if stalled else (f.last_error or "process exited")
                await asyncio.to_thread(f.stop, 5)
                out.ffmpeg = None
                delay = out.policy.next_delay()
                out.retry_at = now + delay
                log.error("Output '%s' failed: %s - restarting in %.0fs", out.name, why, delay)
                continue
            if now < out.retry_at or self._frame_source() is None:
                continue
            if out.name == "recorder":
                crop = await game_crop(self.session.page) if self.session and self.session.page else None
                if crop is None and self.out_cfg.get("recordings", {}).get("clean", True):
                    continue  # wait until the overlay has laid out the game area
                self._crop_holder["value"] = crop
                self._recorder_crop = crop
            try:
                targets = out.make_targets()
                if not targets:
                    out.retry_at = now + 30
                    continue
                out.ffmpeg = FfmpegOutput(self.ffmpeg, targets, *self._out_size, self.out_cfg, ROOT, name=out.name)
                if self.mixer:
                    out.ffmpeg.audio_port = self.mixer.add_sink()
                out.ffmpeg.start(self._frame_source)
                out.policy.mark_started()
                if out.name == "recorder" and self._crop_holder["value"]:
                    w, h, x, y = self._crop_holder["value"]
                    log.info("Recording overlay + clean game video (game area %dx%d at %d,%d)", w, h, x, y)
            except Exception as exc:
                out.ffmpeg = None
                out.retry_at = now + out.policy.next_delay()
                log.error("Could not start output '%s': %s", out.name, exc)

    async def _check_recorder_crop(self) -> None:
        """If the game area moved (layout change), restart the recorder so the
        clean video stays aligned."""
        rec = next((o for o in self.outputs if o.name == "recorder" and o.ffmpeg), None)
        if not rec or not self.session:
            return
        crop = await game_crop(self.session.page)
        if crop and getattr(self, "_recorder_crop", None) and crop_moved(crop, self._recorder_crop):
            log.warning("Game area moved %s -> %s - restarting recorder", self._recorder_crop, crop)
            await asyncio.to_thread(rec.ffmpeg.stop, 5)
            rec.ffmpeg = None
            rec.retry_at = 0

    # ---------- status ----------

    def status(self) -> dict:
        s = self.runner.status() if self.runner else {}
        return {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_s": round(time.monotonic() - self.started_at),
            "game": self.config["name"],
            "mode": self.mode.get("label", self.mode_name),
            "bot": s,
            "browser": {
                "running": bool(self.session and self.session.is_alive()),
                "restarts": self._session_policy.total_restarts,
                "planned_refreshes": self.planned_refreshes,
                "move_s_recent": round(self.runner.recent_move_s, 2) if self.runner and self.runner.recent_move_s else None,
                "move_s_normal": round(self.runner.baseline_move_s, 2) if self.runner and self.runner.baseline_move_s else None,
                "uptime_s": round(time.monotonic() - self._session_started_at) if self.session else 0,
                "last_frame_s": round(self.capture.seconds_since_last_frame(), 1) if self.capture else None,
            },
            "outputs": {
                o.name: {**(o.ffmpeg.stats() if o.ffmpeg else {"running": False}),
                         "restarts": o.policy.total_restarts}
                for o in self.outputs
            },
        }

    def _write_status(self) -> None:
        """Best effort: on Windows another program (antivirus, indexer, an
        editor) can briefly lock the file. That must never stop the stream."""
        path = ROOT / "data" / "status.json"
        tmp = path.with_name(f"status.{os.getpid()}.tmp")
        try:
            data = json.dumps(self.status(), indent=1, default=str)
        except Exception as exc:
            log.warning("Could not build status: %s", exc)
            return
        for attempt in range(3):
            try:
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(path)
                return
            except OSError as exc:
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        log.warning("Could not write %s (file in use?): %s", path.name, last_exc)

    def _heartbeat(self) -> None:
        st = self.status()
        b = st["bot"]
        outs = ", ".join(f"{k}: {'OK' if v.get('running') else 'DOWN'} ({v.get('restarts', 0)} restarts)"
                         for k, v in st["outputs"].items()) or "none"
        br = st["browser"]
        speed = (f"{br['move_s_recent']}s/move (normal {br['move_s_normal']}s)" if br.get("move_s_normal")
                 else "measuring speed")
        log.info("Heartbeat: up %s | run #%s stage %s score %s | %s | browser restarts %d, refreshes %d | outputs: %s",
                 _hms(st["uptime_s"]), b.get("run_id"), b.get("level"), b.get("score"), speed,
                 br["restarts"], br["planned_refreshes"], outs)

    # ---------- main loop ----------

    async def run(self, stop: asyncio.Event) -> int:
        self._stop = stop
        err = self._check_outputs()
        if err:
            log.error("%s", err)
            return 1
        (ROOT / "data").mkdir(exist_ok=True)
        self.db = StatsDB(ROOT / "data" / "stats.db")
        self.server = GameServer(self.config["build_dir"],
                                 mounts={"/__stream__": ROOT / "stream" / "overlay"}).start()
        self._build_outputs()
        if self.outputs:
            await asyncio.to_thread(self._setup_audio)
        janitor = RecordingJanitor(self.out_cfg, ROOT) if self.opts.record else None
        always_on = self.opts.live or self.opts.record
        if always_on:
            keep_awake(True)
        if self.opts.duration:
            asyncio.get_running_loop().call_later(self.opts.duration, stop.set)

        wd = self.wd_cfg
        refresh_s = wd.get("browser_refresh_minutes", 60) * 60
        slow_factor = wd.get("slow_move_factor", 1.6)
        hung_s = wd.get("bot_hung_seconds", 180)
        no_frames_s = wd.get("no_frames_seconds", 30)
        exit_code = 0
        last = {"snap": 0.0, "status": 0.0, "beat": time.monotonic(), "janitor": 0.0, "crop": time.monotonic()}
        log.info("Starting %s in %s mode%s%s%s", self.config["name"], self.mode.get("label", self.mode_name),
                 " | LIVE on YouTube" if self.opts.live else "", " | recording" if self.opts.record else "",
                 " | test file" if self.opts.test_file else "")
        try:
            try:
                await self._start_session(stop)
            except Exception as exc:
                log.exception("Browser session failed to start")
                await self._restart_session(f"start failed: {exc}")

            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    now = time.monotonic()

                    # --- browser session ---
                    if self._session_retry_at is not None:
                        if now >= self._session_retry_at:
                            try:
                                await self._start_session(stop)
                                log.info("Browser session restarted")
                            except Exception as exc:
                                log.exception("Browser session restart failed")
                                await self._restart_session(f"restart failed: {exc}")
                    else:
                        reason = None
                        if self.runner_task and self.runner_task.done():
                            exc = None if self.runner_task.cancelled() else self.runner_task.exception()
                            if exc is None:
                                log.info("Bot finished (%s runs)", self.runner.runs_completed)
                                stop.set()
                                break
                            reason = f"bot stopped: {exc}"
                        elif self.session and not self.session.is_alive():
                            reason = "browser closed or crashed"
                        elif (self.capture and now - self._session_started_at > 60
                              and self.capture.seconds_since_last_frame() > no_frames_s):
                            reason = f"no frames from browser for {no_frames_s}s (frozen)"
                        elif self.runner and self.runner.bot_state == "playing" and now - self.runner.last_step_at > hung_s:
                            reason = f"bot made no move for {hung_s}s (hung)"
                        planned = self._refresh_reason(now, refresh_s, slow_factor) if not reason else None
                        if reason:
                            await self._restart_session(reason)
                        elif planned:
                            await self._planned_refresh(planned)
                        elif self.runner and self.runner.has_active_run and now - last["snap"] >= 5:
                            last["snap"] = now
                            try:
                                snap = await asyncio.wait_for(self.runner.adapter.snapshot(), 5)
                                if snap:
                                    self.snapshot = snap
                            except Exception:
                                pass

                    # --- outputs ---
                    if self.outputs:
                        await self._supervise_outputs()
                        if now - last["crop"] >= 30:
                            last["crop"] = now
                            await self._check_recorder_crop()
                    if janitor and now - last["janitor"] >= 600:
                        last["janitor"] = now
                        await asyncio.to_thread(janitor.clean)

                    # --- status ---
                    if now - last["status"] >= 5:
                        last["status"] = now
                        self._write_status()
                    if now - last["beat"] >= wd.get("heartbeat_minutes", 5) * 60:
                        last["beat"] = now
                        self._heartbeat()
                except Exception:
                    # Keep supervising: a bug in one check (or a transient OS error)
                    # must not take down a 24/7 stream. Rate-limit the log.
                    if time.monotonic() - last.get("err", 0) > 60:
                        last["err"] = time.monotonic()
                        log.exception("Watchdog check failed - continuing")
        except Exception:
            log.exception("Streamer crashed")
            exit_code = 1
        finally:
            await self._shutdown()
            if always_on:
                keep_awake(False)
        return exit_code

    async def _shutdown(self) -> None:
        log.info("Shutting down...")
        if self.runner_task and not self.runner_task.done():
            try:
                await asyncio.wait_for(self.runner_task, 30)   # finishes the current move, saves the run
            except (asyncio.TimeoutError, Exception):
                self.runner_task.cancel()
                await asyncio.gather(self.runner_task, return_exceptions=True)
        for out in self.outputs:
            if out.ffmpeg:
                await asyncio.to_thread(out.ffmpeg.stop, 20)
                if out.name in ("file", "recorder"):
                    for t in out.ffmpeg.targets:
                        where = Path(t.destination).parent if t.kind == "segments" else t.destination
                        log.info("Saved %s in %s", t.label, where)
        if self.mixer:
            self.mixer.stop()
        if self.overlay:
            await self.overlay.stop()
        await self._stop_session()
        if self.server:
            self.server.stop()
        if self.runner:
            log.info("Bot finished: %d run(s), %s high score %d", self.runner.runs_completed,
                     self.mode_name, self.runner.high_score)
        if self.db:
            self._write_status()
            self.db.close()


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
