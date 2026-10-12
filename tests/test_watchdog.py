import time

from core.recordings import RecordingJanitor
from core.watchdog import RestartPolicy


def test_backoff_then_cooldown():
    p = RestartPolicy("x", backoff_s=(1, 2, 3), max_restarts=3, cooldown_s=99)
    assert [p.next_delay() for _ in range(3)] == [1, 2, 3]
    assert p.next_delay() == 99          # 4th restart inside the window -> long cooldown
    assert p.next_delay() == 1           # counting starts again after the cooldown


def test_stable_component_resets_counters():
    p = RestartPolicy("x", backoff_s=(1, 2, 3), stable_after_s=0.01)
    p.next_delay(), p.next_delay()
    p.mark_started()
    time.sleep(0.02)
    assert p.next_delay() == 1


def test_janitor_deletes_oldest_but_never_newest(tmp_path):
    d = tmp_path / "recordings" / "overlay"
    d.mkdir(parents=True)
    files = []
    for i in range(4):
        f = d / f"overlay_{i}.mp4"
        f.write_bytes(b"x" * 1024 * 1024)  # 1 MB
        t = time.time() - 100 + i
        import os
        os.utime(f, (t, t))
        files.append(f)
    cfg = {"recordings": {"dir": "recordings", "max_total_gb": 2.5 / 1024, "min_free_disk_gb": 0}}
    deleted = RecordingJanitor(cfg, tmp_path).clean()
    assert deleted == files[:2]           # 4 MB -> under 2.5 MB by removing the two oldest
    assert files[3].exists()              # the file being written is never deleted
