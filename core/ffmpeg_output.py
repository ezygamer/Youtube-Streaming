"""FFmpeg encoder process: JPEG frames on stdin -> H.264/AAC -> file(s) or RTMP.

One process can write several outputs from the same frames (e.g. the overlay
recording plus a cropped game-only recording), decoding the input only once.

Frames are written by a dedicated thread at a constant rate. (Measured on
Windows: writing ~90 KB frames through an asyncio pipe took ~23 ms each and
blocked the event loop that also runs the bot; a blocking write in a thread
releases the GIL and keeps everything else smooth.)

Secrets: the RTMP URL contains the stream key. Every line we log (the command
line and FFmpeg's own output, which echoes the destination URL) goes through
_redact(), so the key never reaches the console or log files.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("streamer.ffmpeg")


def find_ffmpeg() -> str | None:
    """FFMPEG_PATH env var, then PATH, then the winget install folder
    (winget updates PATH only for terminals opened after the install)."""
    candidates = [os.getenv("FFMPEG_PATH"), shutil.which("ffmpeg")]
    winget = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget.is_dir():
        candidates += sorted((str(p) for p in winget.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe")), reverse=True)
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


@dataclass
class OutputTarget:
    kind: str          # "youtube" | "kick" | "twitch" | "facebook" | "tee" | "file" | "segments"
    destination: str   # RTMP URL (contains the key!), file path, or segment filename pattern
    secret: str = ""   # value to mask in logs
    extra_secrets: tuple[str, ...] = ()   # more values to mask ("tee": one key per platform)
    crop: tuple[int, int, int, int] | None = None   # (w, h, x, y) of the frame to keep
    segment_seconds: int = 3600                      # "segments": length of each file
    video: dict = field(default_factory=dict)        # per-output overrides of cfg["video"]
    vfilter: str = ""                                # extra FFmpeg video filter (after crop)
    label: str = ""


def youtube_target() -> OutputTarget:
    key = os.getenv("YOUTUBE_STREAM_KEY", "").strip()
    base = os.getenv("YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/")
    if not key:
        raise RuntimeError("YOUTUBE_STREAM_KEY is not set. Copy .env.example to .env and add your stream key.")
    return OutputTarget("youtube", f"{base}/{key}", secret=key, label="youtube")


# Kick runs on Amazon IVS, which is stricter than YouTube: a portrait 1080x1920
# feed showed up black/frozen in Kick's player. Kick and Twitch players are
# landscape, so by default the vertical scene is centred in a 1920x1080 frame,
# encoded as H.264 Main profile without B-frames at a constant bitrate (their
# recommended OBS settings). <PREFIX>_LAYOUT=portrait sends it unchanged.
# Bitrate: Kick flagged the session UNSTABLE at 6 Mbit/s (our upload ran at 0.9x
# real time); this mostly static scene looks fine at 3.5 Mbit/s.
_LANDSCAPE = "scale=-2:1080,pad=1920:1080:(ow-iw)/2:0:color=black,setsar=1"


def _platform_target(kind: str, prefix: str, default_url: str, where: str) -> OutputTarget:
    key = os.getenv(f"{prefix}_STREAM_KEY", "").strip()
    base = (os.getenv(f"{prefix}_STREAM_URL", "").strip() or default_url).rstrip("/")
    if not key or not base:
        need = f"{prefix}_STREAM_KEY" if base else f"{prefix}_STREAM_URL and {prefix}_STREAM_KEY"
        raise RuntimeError(f"{need} must be set in .env ({where}).")
    portrait = os.getenv(f"{prefix}_LAYOUT", "landscape").strip().lower() == "portrait"
    bitrate = os.getenv(f"{prefix}_BITRATE", "3500k").strip() or "3500k"
    return OutputTarget(kind, f"{base}/{key}", secret=key, label=kind, vfilter="" if portrait else _LANDSCAPE,
                        video={"profile": "main", "bframes": 0, "cbr": True, "bitrate": bitrate,
                               "maxrate": bitrate, "bufsize": bitrate})


def kick_target() -> OutputTarget:
    # The Kick Stream URL (rtmps://...:443/app) is per channel, so no default.
    return _platform_target("kick", "KICK", "", "kick.com > Creator Dashboard > Settings > Stream Key")


def twitch_target() -> OutputTarget:
    # Twitch picks the nearest ingest server for this URL.
    return _platform_target("twitch", "TWITCH", "rtmp://live.twitch.tv/app",
                            "twitch.tv > Creator Dashboard > Settings > Stream > Primary Stream key")


def facebook_target() -> OutputTarget:
    # Facebook Live accepts RTMPS only (plain rtmp:// is refused).
    return _platform_target("facebook", "FACEBOOK", "rtmps://live-api-s.facebook.com:443/rtmp",
                            "facebook.com > Live Producer > Streaming software > Stream key")


def multi_platform_target(targets: list[OutputTarget]) -> OutputTarget:
    """Send ONE encode to several platforms (FFmpeg's tee muxer).

    Two separate encoders would double the CPU load; the tee muxer encodes once
    and uploads the same packets to each platform. All targets must share video
    settings (Kick and Twitch do). Upload still doubles, so the bitrate comes
    from MULTI_STREAM_BITRATE (default 1800k). Measured on our Wi-Fi: 2 x 2.5 Mbit/s
    ran at 0.9x real time (Kick then buffers; Twitch hides it better), so the
    two uploads together must stay well under the line's ~5.5 Mbit/s. If one platform drops, FFmpeg
    exits and the supervisor restarts both a few seconds later.
    """
    if len({t.vfilter for t in targets}) > 1:
        raise RuntimeError("Streaming to several platforms at once needs the same layout for all of them "
                           "(set KICK_LAYOUT / TWITCH_LAYOUT / FACEBOOK_LAYOUT to the same value).")
    bitrate = os.getenv("MULTI_STREAM_BITRATE", "1800k").strip() or "1800k"
    video = {**targets[0].video, "bitrate": bitrate, "maxrate": bitrate, "bufsize": bitrate}
    slaves = "|".join(f"[f=flv:flvflags=no_duration_filesize]{t.destination}" for t in targets)
    return OutputTarget("tee", slaves, secret=targets[0].secret,
                        extra_secrets=tuple(t.secret for t in targets[1:]),
                        label="+".join(t.label for t in targets), vfilter=targets[0].vfilter, video=video)


def file_target(directory: str | Path, name: str | None = None) -> OutputTarget:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = name or time.strftime("stream_%Y%m%d_%H%M%S.mp4")
    return OutputTarget("file", str(directory / name), label="file")


# FFmpeg chatter that is expected and harmless for JPEG input.
_IGNORED_WARNINGS = ("deprecated pixel format used", "Guessed Channel Layout")


class FfmpegOutput:
    def __init__(self, ffmpeg: str, targets: OutputTarget | list[OutputTarget], width: int, height: int,
                 cfg: dict, root: Path, name: str = "ffmpeg"):
        self.ffmpeg = ffmpeg
        self.targets = targets if isinstance(targets, list) else [targets]
        self.target = self.targets[0]
        self.name = name
        self.width, self.height = width, height
        self.cfg = cfg
        self.fps = cfg.get("fps", 30)
        self.root = root
        self.proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.progress: dict[str, str] = {}
        self.last_progress_at = 0.0
        self.started_at = 0.0
        self.last_error = ""
        self.frames_written = 0
        self.frames_late = 0
        self.write_seconds = 0.0   # time spent blocked writing to FFmpeg (diagnostics)
        # When set, audio comes from the AudioMixer over this local TCP port
        # (game sounds + music) instead of silence / a music file.
        self.audio_port: int | None = None

    # ---------- command line ----------

    def _audio_input(self) -> tuple[list[str], list[str]]:
        if self.audio_port:
            return ["-thread_queue_size", "1024", "-f", "s16le", "-ar", "44100", "-ac", "2",
                    "-i", f"tcp://127.0.0.1:{self.audio_port}?listen=1"], []
        a = self.cfg.get("audio", {})
        music = a.get("file")
        if a.get("source") == "file" and music:
            music_path = (self.root / music) if not Path(music).is_absolute() else Path(music)
            if music_path.is_file():
                return ["-stream_loop", "-1", "-i", str(music_path)], ["-af", f"volume={a.get('volume', 0.35)}"]
            log.warning("Music file %s not found - using silence", music_path)
        # YouTube expects an audio track; use silence if there's no music.
        return ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"], []

    def _video_args(self, t: OutputTarget) -> list[str]:
        v = {**self.cfg.get("video", {}), **t.video}
        gop = int(self.fps * v.get("keyframe_seconds", 2))
        codec = v.get("codec", "libx264")
        args = ["-c:v", codec, "-pix_fmt", "yuv420p", "-r", str(self.fps), "-g", str(gop), "-keyint_min", str(gop)]
        if v.get("profile"):
            args += ["-profile:v", v["profile"]]
        if "bframes" in v:
            args += ["-bf", str(v["bframes"])]
        if v.get("cbr") and codec == "libx264":
            args += ["-x264-params", "nal-hrd=cbr"]
        if "crf" in v:
            # Quality-based (recordings): small files for this mostly static scene.
            args += ["-crf", str(v["crf"])]
            if v.get("maxrate"):
                args += ["-maxrate", v["maxrate"], "-bufsize", v.get("bufsize", v["maxrate"])]
        else:
            # Steady bitrate (live streaming).
            args += ["-b:v", v.get("bitrate", "6000k"), "-maxrate", v.get("maxrate", v.get("bitrate", "6000k")),
                     "-bufsize", v.get("bufsize", "12000k")]
        if codec == "libx264":
            args += ["-preset", v.get("preset", "veryfast"), "-sc_threshold", "0"]
        return args + list(v.get("extra_args", []))

    @staticmethod
    def _muxer_args(t: OutputTarget) -> list[str]:
        # Fragmented MP4 stays playable even if the process is killed.
        frag = "+frag_keyframe+empty_moov+default_base_moof"
        if t.kind == "tee":
            # FLV needs the H.264/AAC headers up front; the tee muxer won't ask for them itself.
            return ["-flags", "+global_header", "-f", "tee", t.destination]
        if t.kind in ("youtube", "kick", "twitch", "facebook"):
            return ["-f", "flv", "-flvflags", "no_duration_filesize", t.destination]
        if t.kind == "segments":
            return ["-f", "segment", "-segment_time", str(t.segment_seconds), "-reset_timestamps", "1",
                    "-strftime", "1", "-segment_format", "mp4",
                    "-segment_format_options", f"movflags={frag}", t.destination]
        return ["-movflags", frag, "-f", "mp4", "-y", t.destination]

    def build_args(self) -> list[str]:
        args = [self.ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
                "-nostats", "-progress", "pipe:2", "-stats_period", "5",
                # input 0: JPEG frames from the browser at a fixed rate
                # We know the input format: skip FFmpeg's multi-second analysis
                # so it opens the audio input right away (keeps A/V aligned).
                "-thread_queue_size", "512", "-probesize", "1M", "-analyzeduration", "0",
                "-f", "image2pipe", "-framerate", str(self.fps), "-c:v", "mjpeg", "-i", "pipe:0"]
        audio_in, audio_filter = self._audio_input()
        args += audio_in

        # One decoded input, split into one video stream per output (cropped if asked).
        n = len(self.targets)
        if n == 1 and not self.target.crop and not self.target.vfilter:
            video_maps = ["0:v"]
        else:
            chains = [f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n))] if n > 1 else []
            video_maps = []
            for i, t in enumerate(self.targets):
                src = f"[s{i}]" if n > 1 else "[0:v]"
                filters = []
                if t.crop:
                    w, h, x, y = t.crop
                    filters.append(f"crop={w}:{h}:{x}:{y}")
                if t.vfilter:
                    filters.append(t.vfilter)
                if filters:
                    chains.append(f"{src}{','.join(filters)}[v{i}]")
                    video_maps.append(f"[v{i}]")
                else:
                    video_maps.append(src)
            args += ["-filter_complex", ";".join(chains)]

        a = self.cfg.get("audio", {})
        for t, vmap in zip(self.targets, video_maps):
            args += ["-map", vmap, "-map", "1:a", *self._video_args(t),
                     "-c:a", "aac", "-b:a", a.get("bitrate", "128k"), "-ar", "44100", "-ac", "2", *audio_filter,
                     # The audio input never ends; stop when the video (stdin) ends.
                     "-shortest", *self._muxer_args(t)]
        return args

    def _redact(self, text: str) -> str:
        for t in self.targets:
            for secret in (t.secret, *t.extra_secrets):
                if secret:
                    text = text.replace(secret, "****")
        return text

    # ---------- lifecycle ----------

    def start(self, frame_source: Callable[[], bytes | None]) -> None:
        """Start FFmpeg and the writer thread. frame_source() returns the
        latest JPEG frame (or None before the first frame)."""
        args = self.build_args()
        log.info("Starting FFmpeg [%s] -> %s: %s", self.name, ", ".join(t.label or t.kind for t in self.targets),
                 self._redact(" ".join(args[1:])))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, bufsize=1024 * 1024, creationflags=flags)
        self.started_at = time.monotonic()
        self.last_error = ""
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._read_stderr, name=f"{self.name}-stderr", daemon=True),
            threading.Thread(target=self._write_frames, args=(frame_source,), name=f"{self.name}-writer", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def is_healthy(self) -> bool:
        writer = self._threads[1] if len(self._threads) > 1 else None
        return self.is_running() and writer is not None and writer.is_alive()

    def _write_frames(self, frame_source: Callable[[], bytes | None]) -> None:
        interval = 1 / self.fps
        next_at = time.monotonic()
        while not self._stop.is_set():
            frame = frame_source()
            if frame is not None:
                try:
                    t0 = time.monotonic()
                    self.proc.stdin.write(frame)
                    self.proc.stdin.flush()
                    self.write_seconds += time.monotonic() - t0
                    self.frames_written += 1
                except (BrokenPipeError, OSError, ValueError) as exc:
                    if not self._stop.is_set():
                        self.last_error = self.last_error or f"pipe closed: {exc}"
                        log.error("FFmpeg [%s] stopped accepting frames: %s", self.name, self.last_error)
                    return
            next_at += interval
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # Behind schedule: write the next frames straight away to catch
                # up. Dropping frames would make the video shorter than the
                # audio (sounds drifting after the moves), so only give up and
                # resync after a long stall.
                self.frames_late += 1
                if delay < -5:
                    log.warning("FFmpeg [%s] fell %.0fs behind - resyncing (A/V may shift)", self.name, -delay)
                    next_at = time.monotonic()

    def stop(self, timeout: float = 10) -> None:
        """Blocking: stop writing, let FFmpeg finish the output, then exit."""
        if not self.proc:
            return
        self._stop.set()
        if len(self._threads) > 1:
            self._threads[1].join(timeout=2)
        try:
            self.proc.stdin.close()  # EOF -> FFmpeg flushes and finalizes the output
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("FFmpeg [%s] did not exit in %ss - killing it", self.name, timeout)
            self.proc.kill()
            self.proc.wait()
        if self._threads:
            self._threads[0].join(timeout=2)
        log.info("FFmpeg [%s] stopped (exit code %s): %d frames written, %d late",
                 self.name, self.proc.returncode, self.frames_written, self.frames_late)

    def _read_stderr(self) -> None:
        """-progress writes key=value lines; anything else is a warning/error."""
        for raw in self.proc.stderr:
            text = self._redact(raw.decode("utf-8", "replace").strip())
            if not text:
                continue
            key, sep, value = text.partition("=")
            if sep and " " not in key:
                self.progress[key] = value.strip()
                if key == "progress":
                    self.last_progress_at = time.monotonic()
            elif not any(w in text for w in _IGNORED_WARNINGS):
                self.last_error = text
                log.warning("ffmpeg [%s]: %s", self.name, text)

    def stats(self) -> dict:
        p = self.progress
        return {
            "running": self.is_running(),
            "fps": p.get("fps"),
            "bitrate": p.get("bitrate"),
            "speed": p.get("speed"),
            "frames": p.get("frame"),
            "dropped": p.get("drop_frames"),
            "written": self.frames_written,
            "late": self.frames_late,
            "avg_write_ms": round(1000 * self.write_seconds / self.frames_written, 1) if self.frames_written else None,
            "uptime_s": round(time.monotonic() - self.started_at) if self.started_at else 0,
            "last_error": self.last_error,
        }
