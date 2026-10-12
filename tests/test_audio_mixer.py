"""AudioMixer mixing logic (no FFmpeg needed: sounds are injected directly)."""
import socket
import time

import numpy as np

from core.audio_mixer import BLOCK_FRAMES, AudioMixer


def make_mixer(music=None):
    m = AudioMixer.__new__(AudioMixer)
    AudioMixer.__init__(m, ffmpeg="ffmpeg", sound_files={})
    m.sounds = {"beep": np.full((BLOCK_FRAMES * 3, 2), 0.5, dtype=np.float32)}
    m.music = music
    return m


def pcm(block: bytes) -> np.ndarray:
    return np.frombuffer(block, dtype="<i2").reshape(-1, 2)


def test_silence_when_nothing_plays():
    m = make_mixer()
    assert not pcm(m.mix_block()).any()


def test_sound_plays_for_its_length_then_stops():
    m = make_mixer()
    m.play("beep")
    blocks = [pcm(m.mix_block()) for _ in range(4)]
    assert all(abs(b[0, 0] - 16383) <= 1 for b in blocks[:3])   # 0.5 * 32767
    assert not blocks[3].any()
    assert m.played == {"beep": 1}


def test_replaying_a_sound_restarts_it_instead_of_stacking():
    # Like the game's <audio> element: a new play starts it over, never 3 copies at once.
    m = make_mixer()
    m.play("beep"), m.play("beep"), m.play("beep")
    assert abs(pcm(m.mix_block())[0, 0] - 16383) <= 1
    m.mix_block()
    m.play("beep")                     # restart: full length again from here
    blocks = [pcm(m.mix_block()) for _ in range(4)]
    assert all(b.any() for b in blocks[:3]) and not blocks[3].any()


def test_different_sounds_mix_and_clip():
    m = make_mixer()
    m.sounds["boop"] = np.full((BLOCK_FRAMES, 2), 0.8, dtype=np.float32)
    m.play("beep"), m.play("boop")     # 0.5 + 0.8 -> clipped to full scale
    assert pcm(m.mix_block())[0, 0] == 32767


def test_unknown_sound_is_ignored():
    m = make_mixer()
    m.play("nope")
    assert not pcm(m.mix_block()).any()


def test_music_loops_seamlessly():
    music = np.arange(BLOCK_FRAMES * 2 + 100, dtype=np.float32).repeat(2).reshape(-1, 2) / 1e5
    m = make_mixer(music)
    out = np.concatenate([pcm(m.mix_block()) for _ in range(3)])
    expected = np.concatenate([music, music])[: BLOCK_FRAMES * 3]
    assert np.allclose(out[:, 0] / 32767, expected[:, 0], atol=1e-4)


def test_blocks_reach_a_listening_sink():
    m = make_mixer()
    port = m.add_sink()
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    m.start()
    try:
        conn, _ = srv.accept()
        conn.settimeout(2)
        m.play("beep")
        got = b""
        deadline = time.time() + 2
        while len(got) < BLOCK_FRAMES * 4 * 10 and time.time() < deadline:
            got += conn.recv(65536)
        assert len(got) >= BLOCK_FRAMES * 4 * 10
        assert pcm(got[: len(got) // 4 * 4]).any()   # the beep arrived
        conn.close()
    finally:
        m.stop()
        srv.close()
