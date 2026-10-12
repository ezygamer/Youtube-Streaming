"""Real-time audio for the stream: the game's own sound effects + optional music.

Why not capture the browser's audio? The bot's Chromium runs headless, with no
sound device, so the game's sounds are never actually played anywhere; system
loopback capture would also pick up every other sound on the PC. Instead the
bot knows the exact moment of every move, so it triggers the game's own sound
files itself and this mixer writes them into the stream.

How it works:
  - each sound file (and the music) is decoded once with FFmpeg into 16-bit
    stereo PCM at 44.1 kHz, held in memory as numpy arrays;
  - a thread produces 20 ms blocks in real time: music (looped) + every
    sound currently playing, mixed and clipped;
  - every FFmpeg output (YouTube, recorder) gets the same blocks through its
    own local TCP socket ("sink"), as its audio input.
"""
import logging
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("streamer.audio")

RATE = 44100
CHANNELS = 2
BLOCK_S = 0.02
BLOCK_FRAMES = int(RATE * BLOCK_S)          # 882 sample frames = 20 ms
BYTES_PER_FRAME = 2 * CHANNELS
# Sound effects are normalised to this peak (-6 dBFS, so two at once never clip) before their
# volume is applied: the game's files differ by ~19 dB (its bell is barely
# audible next to its match sound).
SFX_PEAK = 0.5


def decode(ffmpeg: str, path: Path) -> np.ndarray:
    """Any audio file -> float32 array of shape (frames, 2), range -1..1."""
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-"],
        capture_output=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return np.frombuffer(out, dtype="<i2").reshape(-1, CHANNELS).astype(np.float32) / 32768.0


def fetch_sounds(sounds: dict[str, str], cache_dir: Path) -> dict[str, Path]:
    """Download the game's sound files once (they are the game's own public
    assets) into a local cache, so streaming never depends on those URLs."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, url in sounds.items():
        ext = Path(url.split("?")[0]).suffix or ".mp3"
        path = cache_dir / f"{name}{ext}"
        if not path.is_file() or path.stat().st_size == 0:
            try:
                log.info("Downloading game sound '%s' from %s", name, url)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    path.write_bytes(resp.read())
            except Exception as exc:
                log.warning("Could not download sound '%s': %s", name, exc)
                continue
        paths[name] = path
    return paths


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Sink:
    """One FFmpeg audio input, fed over a local TCP connection."""

    def __init__(self, port: int):
        self.port = port
        self.sock: socket.socket | None = None
        # FFmpeg starts reading video the moment it starts, but opens its audio
        # input a little later. The silence sent first on connect covers exactly
        # that gap, so audio time == video time and sounds line up with moves.
        self.started_at = time.monotonic()
        self.connect_until = self.started_at + 20
        self.dead = False

    def try_connect(self) -> None:
        if self.sock or self.dead:
            return
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            s.settimeout(10)
            gap_frames = int((time.monotonic() - self.started_at) * RATE)
            s.sendall(bytes(gap_frames * BYTES_PER_FRAME))   # silence covering the gap
            self.sock = s
            log.debug("Audio connected on port %d after %.2fs", self.port, gap_frames / RATE)
        except OSError:
            if time.monotonic() > self.connect_until:
                log.warning("Audio: FFmpeg never accepted the audio connection on port %d", self.port)
                self.dead = True

    def send(self, data: bytes) -> None:
        if not self.sock:
            return
        try:
            self.sock.sendall(data)
        except OSError:
            # FFmpeg exited (it is restarted separately, with a new sink).
            self.close()
            self.dead = True

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class AudioMixer:
    def __init__(self, ffmpeg: str, sound_files: dict[str, Path], volumes: dict[str, float] | None = None,
                 game_volume: float = 1.0, music_file: Path | None = None, music_volume: float = 0.35):
        self.sounds: dict[str, np.ndarray] = {}
        volumes = volumes or {}
        for name, path in sound_files.items():
            try:
                samples = decode(ffmpeg, path)
                peak = float(np.abs(samples).max()) if len(samples) else 0.0
                if peak > 0:
                    samples = samples * (SFX_PEAK / peak)
                self.sounds[name] = samples * game_volume * volumes.get(name, 1.0)
            except Exception as exc:
                log.warning("Could not decode sound '%s' (%s): %s", name, path, exc)
        self.music: np.ndarray | None = None
        if music_file and music_file.is_file():
            try:
                self.music = decode(ffmpeg, music_file) * music_volume
                log.info("Music: %s (%.0f s, volume %.0f%%)", music_file.name, len(self.music) / RATE, music_volume * 100)
            except Exception as exc:
                log.warning("Could not decode music %s: %s", music_file, exc)
        elif music_file:
            log.info("No music file at %s - game sounds only", music_file)
        log.info("Game sounds loaded: %s", ", ".join(sorted(self.sounds)) or "none")
        self._music_pos = 0
        self._voices: list[list] = []          # [name, samples, position]
        self._pending: list[str] = []
        self._sinks: list[_Sink] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.played: dict[str, int] = {}

    # ---------- public ----------

    def play(self, name: str) -> None:
        """Thread-safe: start a sound now (unknown names are ignored)."""
        if name in self.sounds:
            with self._lock:
                self._pending.append(name)

    def add_sink(self) -> int:
        """Register a new FFmpeg audio input; returns the TCP port FFmpeg must
        listen on (FFmpeg connects to nothing: it listens, we connect)."""
        sink = _Sink(free_port())
        with self._lock:
            self._sinks = [s for s in self._sinks if not s.dead]
            self._sinks.append(sink)
        return sink.port

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-mixer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        for s in self._sinks:
            s.close()

    # ---------- mixing ----------

    def mix_block(self) -> bytes:
        """Next 20 ms of audio as 16-bit stereo PCM bytes."""
        out = np.zeros((BLOCK_FRAMES, CHANNELS), dtype=np.float32)
        if self.music is not None and len(self.music):
            take = 0
            while take < BLOCK_FRAMES:  # loop the music seamlessly
                n = min(BLOCK_FRAMES - take, len(self.music) - self._music_pos)
                out[take:take + n] += self.music[self._music_pos:self._music_pos + n]
                take += n
                self._music_pos = (self._music_pos + n) % len(self.music)
        with self._lock:
            for name in self._pending:
                # Like the game (audio.currentTime = 0; play()): playing a sound
                # again restarts it instead of stacking another copy on top.
                self._voices = [v for v in self._voices if v[0] != name]
                self._voices.append([name, self.sounds[name], 0])
                self.played[name] = self.played.get(name, 0) + 1
            self._pending.clear()
        alive = []
        for voice in self._voices:
            _, samples, pos = voice
            n = min(BLOCK_FRAMES, len(samples) - pos)
            out[:n] += samples[pos:pos + n]
            voice[2] = pos + n
            if voice[2] < len(samples):
                alive.append(voice)
        self._voices = alive
        return (np.clip(out, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    def _run(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            block = self.mix_block()
            with self._lock:
                sinks = list(self._sinks)
            for sink in sinks:
                sink.try_connect()
                sink.send(block)
            next_at += BLOCK_S
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1:
                next_at = time.monotonic()  # fell far behind (PC asleep?) - resync
