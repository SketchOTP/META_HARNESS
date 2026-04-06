from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from meta_harness.config import load_config


def test_daemon_pauses_on_pause_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import daemon as dm

    cfg = load_config(tmp_path)
    p = cfg.daemon_pause_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pause")
    sleeps: list[float] = []

    def sleep_remove(s: float) -> None:
        sleeps.append(s)
        p.unlink(missing_ok=True)

    monkeypatch.setattr(dm.time, "sleep", sleep_remove)
    monkeypatch.setattr(dm, "_running", True)
    dm._wait_until_unpaused(cfg)
    assert sleeps
    assert 5 in sleeps or sleeps[0] == 5
    assert not p.exists()


def test_daemon_resumes_on_file_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import daemon as dm

    cfg = load_config(tmp_path)
    p = cfg.daemon_pause_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pause")
    monkeypatch.setattr(dm.time, "sleep", lambda s: p.unlink(missing_ok=True))
    monkeypatch.setattr(dm, "_running", True)
    dm._wait_until_unpaused(cfg)
    assert not p.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery in test harness is unreliable on Windows")
def test_daemon_stops_on_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import signal
    import threading
    import time

    from meta_harness import daemon as dm

    (tmp_path / "metaharness.toml").write_text("[cycle]\ninterval_seconds = 1\n", encoding="utf-8")
    cfg = load_config(tmp_path)

    monkeypatch.setattr(dm, "run_cycle", lambda c: time.sleep(0.05))

    def send():
        time.sleep(0.2)
        signal.raise_signal(signal.SIGINT)

    threading.Thread(target=send, daemon=True).start()
    dm._running = True
    dm.run_daemon(cfg)
    assert dm._running is False


def test_next_scheduled_time_past_slot_rolls_to_next_day():
    from meta_harness.daemon import _next_scheduled_time

    now = datetime(2026, 4, 4, 14, 30, 0)
    n = _next_scheduled_time(["07:00"], now=now)
    assert n == datetime(2026, 4, 5, 7, 0, 0)


def test_next_scheduled_time_multiple_returns_nearest():
    from meta_harness.daemon import _next_scheduled_time

    now = datetime(2026, 4, 4, 14, 0, 0)
    n = _next_scheduled_time(["07:00", "22:00"], now=now)
    assert n == datetime(2026, 4, 4, 22, 0, 0)


def test_next_scheduled_time_empty_schedule_raises():
    from meta_harness.daemon import _next_scheduled_time

    with pytest.raises(ValueError, match="non-empty"):
        _next_scheduled_time([], now=datetime(2026, 1, 1, 12, 0, 0))


def test_daemon_rejects_zero_interval_without_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from meta_harness import daemon as dm

    captured: list[str] = []

    def capture_print(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(dm.console, "print", capture_print)
    cfg = load_config(tmp_path)
    assert cfg.cycle.interval_seconds == 0
    assert cfg.cycle.schedule == []
    dm._running = True
    dm.run_daemon(cfg)
    assert any("interval_seconds" in s or "schedule" in s for s in captured)


def test_daemon_starts_product_thread_when_product_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from meta_harness import daemon as dm

    (tmp_path / "metaharness.toml").write_text(
        "[cycle]\ninterval_seconds = 60\n"
        "[product]\nenabled = true\ncatch_up = true\nschedule = [\"23:58\"]\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    created: list[dict] = []

    def _fake_thread(*a, **kw):
        created.append(kw)

        class _T:
            def start(self) -> None:
                pass

        return _T()

    monkeypatch.setattr(dm.threading, "Thread", _fake_thread)
    monkeypatch.setattr(dm, "run_cycle", lambda c: None)
    n = {"i": 0}

    def stop_after_maintenance_sleep(total_seconds: float, c) -> None:
        n["i"] += 1
        if n["i"] >= 1:
            dm._running = False

    monkeypatch.setattr(dm, "_interruptible_sleep", stop_after_maintenance_sleep)
    dm._running = True
    msgs: list[str] = []

    monkeypatch.setattr(dm.console, "print", lambda *a, **k: msgs.append(" ".join(str(x) for x in a)))
    dm.run_daemon(cfg)
    names = [c.get("name") for c in created]
    assert "metaharness-product-agent" in names
    assert any("Product Agent thread" in m for m in msgs)


def test_daemon_schedule_mode_skips_interval_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Non-empty schedule allows interval_seconds = 0."""
    from meta_harness import daemon as dm

    (tmp_path / "metaharness.toml").write_text(
        '[cycle]\ninterval_seconds = 0\ncatch_up = true\nschedule = ["23:59"]\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    monkeypatch.setattr(dm, "run_cycle", lambda c: None)
    monkeypatch.setattr(dm, "_interruptible_sleep", lambda s, c: setattr(dm, "_running", False))
    monkeypatch.setattr(dm, "_running", True)
    msgs: list[str] = []

    monkeypatch.setattr(dm.console, "print", lambda *a, **k: msgs.append(" ".join(str(x) for x in a)))

    dm.run_daemon(cfg)
    assert any("Schedule" in m for m in msgs)
