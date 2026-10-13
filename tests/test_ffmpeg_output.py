"""FFmpeg command building and secret handling (no FFmpeg process started)."""
from pathlib import Path

import pytest

from core.ffmpeg_output import FfmpegOutput, OutputTarget, file_target, kick_target, multi_platform_target, twitch_target, youtube_target

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


def test_kick_target_needs_url_and_key_and_masks_key(monkeypatch):
    monkeypatch.setenv("KICK_STREAM_KEY", "sk_test_secret")
    monkeypatch.delenv("KICK_STREAM_URL", raising=False)
    with pytest.raises(RuntimeError, match="KICK_STREAM_URL"):
        kick_target()
    monkeypatch.setenv("KICK_STREAM_URL", "rtmps://ingest.example:443/app/")
    t = kick_target()
    assert t.destination == "rtmps://ingest.example:443/app/sk_test_secret"
    out = make(t)
    cmd = " ".join(out.build_args())
    assert "-f flv" in cmd
    assert "sk_test_secret" not in out._redact(cmd)
    # Landscape 1920x1080 with the vertical scene centred; Main profile, no B-frames.
    assert "-b:v 3500k" in cmd and "pad=1920:1080" in cmd and "-profile:v main" in cmd and "-bf 0" in cmd
    monkeypatch.setenv("KICK_LAYOUT", "portrait")
    assert "pad=" not in " ".join(make(kick_target()).build_args())


def test_twitch_target_default_ingest(monkeypatch):
    monkeypatch.delenv("TWITCH_STREAM_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TWITCH_STREAM_KEY"):
        twitch_target()
    monkeypatch.setenv("TWITCH_STREAM_KEY", "live_123_secret")
    t = twitch_target()
    assert t.destination == "rtmp://live.twitch.tv/app/live_123_secret"
    cmd = " ".join(make(t).build_args())
    assert "-f flv" in cmd and "pad=1920:1080" in cmd and "-b:v 3500k" in cmd


def test_kick_and_twitch_share_one_encode(monkeypatch):
    monkeypatch.setenv("KICK_STREAM_URL", "rtmps://kick.example:443/app")
    monkeypatch.setenv("KICK_STREAM_KEY", "sk_kick_secret")
    monkeypatch.setenv("TWITCH_STREAM_KEY", "live_twitch_secret")
    monkeypatch.delenv("MULTI_STREAM_BITRATE", raising=False)
    t = multi_platform_target([kick_target(), twitch_target()])
    out = make(t)
    args = out.build_args()
    cmd = " ".join(args)
    assert cmd.count("-c:v libx264") == 1                 # encoded once
    assert "-f tee" in cmd and "-b:v 2500k" in cmd and "pad=1920:1080" in cmd
    assert args[-1] == ("[f=flv:flvflags=no_duration_filesize]rtmps://kick.example:443/app/sk_kick_secret|"
                        "[f=flv:flvflags=no_duration_filesize]rtmp://live.twitch.tv/app/live_twitch_secret")
    red = out._redact(cmd)
    assert "sk_kick_secret" not in red and "live_twitch_secret" not in red


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
