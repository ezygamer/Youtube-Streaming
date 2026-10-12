"""FFmpeg command building and secret handling (no FFmpeg process started)."""
from pathlib import Path

import pytest

from core.ffmpeg_output import FfmpegOutput, OutputTarget, file_target, youtube_target

CFG = {"fps": 30, "video": {"codec": "libx264", "bitrate": "6000k"}, "audio": {"source": "silent"}}


def make(target):
    return FfmpegOutput("ffmpeg", target, 1080, 1920, CFG, Path("."))


def test_youtube_target_requires_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_STREAM_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YOUTUBE_STREAM_KEY"):
        youtube_target()


def test_youtube_target_builds_url_and_masks_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_STREAM_KEY", "abcd-1234-secret")
    monkeypatch.setenv("YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2/")
    t = youtube_target()
    assert t.destination == "rtmp://a.rtmp.youtube.com/live2/abcd-1234-secret"
    out = make(t)
    cmd = " ".join(out.build_args())
    assert "-f flv" in cmd and t.destination in cmd
    assert "abcd-1234-secret" not in out._redact(cmd)
    assert "abcd-1234-secret" not in out._redact("Output #0, flv, to 'rtmp://x/live2/abcd-1234-secret':")


def test_stream_settings(tmp_path):
    args = make(file_target(tmp_path, "x.mp4")).build_args()
    cmd = " ".join(args)
    for expected in ("-c:v libx264", "-pix_fmt yuv420p", "-r 30", "-g 60", "-b:v 6000k",
                     "-c:a aac", "-ar 44100", "-shortest", "anullsrc"):
        assert expected in cmd, expected
    assert args[-1].endswith("x.mp4")


def test_missing_music_falls_back_to_silence(tmp_path):
    cfg = {**CFG, "audio": {"source": "file", "file": str(tmp_path / "nope.mp3")}}
    out = FfmpegOutput("ffmpeg", OutputTarget("file", str(tmp_path / "x.mp4")), 1080, 1920, cfg, tmp_path)
    assert "anullsrc" in " ".join(out.build_args())
