"""Local recordings for 24/7 operation.

Two outputs from one FFmpeg "recorder" process (separate from the YouTube
process, so a YouTube disconnect never interrupts the recordings):

  recordings/overlay/overlay_YYYYMMDD_HHMMSS.mp4  - exactly what viewers see (1080x1920)
  recordings/clean/game_YYYYMMDD_HHMMSS.mp4       - only the game area, no overlay

Files are cut into segments (default 1 hour) and the janitor deletes the
oldest ones when the folder grows past its size limit or the disk gets full.
"""
import logging
import shutil
from pathlib import Path

from playwright.async_api import Page

from core.ffmpeg_output import OutputTarget

log = logging.getLogger("streamer.recordings")

# Visible game area: the scaled iframe, clipped to the inside of its frame border.
_GAME_RECT_JS = """() => {
  const g = document.getElementById('game').getBoundingClientRect();
  const wrap = document.getElementById('frame-wrap');
  const w = wrap.getBoundingClientRect();
  const left = Math.max(g.left, w.left + wrap.clientLeft), top = Math.max(g.top, w.top + wrap.clientTop);
  const right = Math.min(g.right, w.left + wrap.clientLeft + wrap.clientWidth);
  const bottom = Math.min(g.bottom, w.top + wrap.clientTop + wrap.clientHeight);
  return [left, top, right - left, bottom - top];
}"""

# Pixels trimmed from each edge of the game area, so the frame border and its
# anti-aliased rounded corners never show in the clean video.
_SHAVE = 6
# Changes smaller than this are sub-pixel layout noise, not a real move.
CROP_TOLERANCE = 6


def _even(n: float) -> int:
    # H.264 with yuv420p needs even dimensions/offsets.
    return int(n) // 2 * 2


async def game_crop(page: Page) -> tuple[int, int, int, int] | None:
    """(w, h, x, y) of the game area in the 1080x1920 scene, or None if the
    overlay isn't loaded."""
    try:
        x, y, w, h = await page.evaluate(_GAME_RECT_JS)
    except Exception:
        return None
    if w < 50 or h < 50:
        return None
    x, y = round(x) + _SHAVE, round(y) + _SHAVE
    return _even(w - 2 * _SHAVE), _even(h - 2 * _SHAVE), _even(x + 1), _even(y + 1)


def crop_moved(a, b) -> bool:
    return any(abs(p - q) > CROP_TOLERANCE for p, q in zip(a, b))


def recording_targets(cfg: dict, root: Path, crop: tuple[int, int, int, int] | None) -> list[OutputTarget]:
    rec = cfg.get("recordings", {})
    base = root / rec.get("dir", "recordings")
    seconds = int(rec.get("segment_minutes", 60) * 60)
    video = rec.get("video", {"crf": 23, "maxrate": "6000k", "bufsize": "12000k"})
    targets = []
    if rec.get("overlay", True):
        (base / "overlay").mkdir(parents=True, exist_ok=True)
        targets.append(OutputTarget("segments", str(base / "overlay" / "overlay_%Y%m%d_%H%M%S.mp4"),
                                    segment_seconds=seconds, video=video, label="overlay recording"))
    if rec.get("clean", True):
        if crop:
            (base / "clean").mkdir(parents=True, exist_ok=True)
            targets.append(OutputTarget("segments", str(base / "clean" / "game_%Y%m%d_%H%M%S.mp4"),
                                        crop=crop, segment_seconds=seconds, video=video, label="clean game recording"))
        else:
            log.warning("Game area not found - clean game recording disabled for now")
    return targets


class RecordingJanitor:
    """Keeps the recordings folder under a size limit and the disk from filling up."""

    def __init__(self, cfg: dict, root: Path):
        rec = cfg.get("recordings", {})
        self.dir = root / rec.get("dir", "recordings")
        self.max_bytes = rec.get("max_total_gb", 60) * 1024 ** 3
        self.min_free_bytes = rec.get("min_free_disk_gb", 20) * 1024 ** 3

    def _files(self) -> list[Path]:
        return sorted(self.dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime)

    def clean(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        files = self._files()
        # Never touch the newest file of each folder: FFmpeg is writing it.
        newest = {}
        for f in files:
            newest[f.parent] = f
        deletable = [f for f in files if newest.get(f.parent) != f]
        total = sum(f.stat().st_size for f in files)
        deleted = []
        for f in deletable:
            free = shutil.disk_usage(self.dir).free
            if total <= self.max_bytes and free >= self.min_free_bytes:
                break
            size = f.stat().st_size
            try:
                f.unlink()
            except OSError as exc:
                log.warning("Could not delete %s: %s", f, exc)
                continue
            total -= size
            deleted.append(f)
            log.info("Deleted old recording %s (%.0f MB) to free space", f.name, size / 1024 ** 2)
        return deleted
