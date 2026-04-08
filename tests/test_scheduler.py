"""Tests for the Scheduler — pause/resume bookkeeping and config sync.

These tests use a real ``QCoreApplication`` event loop and very short
intervals (a few milliseconds) so we can run them headless.
"""

from __future__ import annotations

import os
import sys
import time

# Force offscreen platform BEFORE importing PyQt6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QTimer

from flarereminder.config import AppConfig, GlobalSettings, Reminder, SuspendState
from flarereminder.scheduler import Scheduler


@pytest.fixture(scope="module")
def qapp():
    a = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield a


def _wait_ms(ms: int) -> None:
    """Spin the event loop for ``ms`` milliseconds."""
    deadline = time.monotonic() + ms / 1000.0
    app = QCoreApplication.instance()
    assert app is not None
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)


def _make_config(*reminders: Reminder) -> AppConfig:
    return AppConfig(
        reminders=list(reminders),
        settings=GlobalSettings(),
        suspend=SuspendState(),
    )


def test_initial_install_starts_enabled_timers(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=1),
    )
    sched = Scheduler(cfg)
    assert "A" in sched._timers
    assert sched._timers["A"].qtimer.isActive()
    sched._timers["A"].qtimer.stop()


def test_disabled_reminder_does_not_start(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=1, enabled=False),
    )
    sched = Scheduler(cfg)
    assert not sched._timers["A"].qtimer.isActive()


def test_pause_then_resume_preserves_remaining(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    rt = sched._timers["A"]
    rt.qtimer.stop()
    rt.qtimer.start(2000)  # 2 seconds for the test
    _wait_ms(50)
    sched.pause("test")
    remembered = rt.remaining_ms
    assert 1500 < remembered <= 2000
    _wait_ms(200)  # Pretend wall time passes
    sched.resume("test")
    assert rt.qtimer.isActive()
    # Remaining should be close to what we remembered before the pause.
    assert abs(rt.qtimer.remainingTime() - remembered) < 200
    rt.qtimer.stop()


def test_multiple_reasons_keep_suspend_until_all_clear(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    sched.pause("idle")
    sched.pause("lock")
    assert sched.is_suspended()
    sched.resume("idle")
    assert sched.is_suspended()  # still locked
    sched.resume("lock")
    assert not sched.is_suspended()
    sched._timers["A"].qtimer.stop()


def test_acknowledge_restarts_from_now(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    rt = sched._timers["A"]
    rt.qtimer.stop()
    rt.qtimer.start(50)
    _wait_ms(70)
    qapp.processEvents()
    # Now timer has fired
    assert rt.in_flight is True
    sched.acknowledge("A")
    assert rt.in_flight is False
    # Should have restarted with the full 10-minute interval.
    assert rt.qtimer.remainingTime() > 500_000
    rt.qtimer.stop()


def test_fire_emits_signal(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    rt = sched._timers["A"]
    received: list[str] = []
    sched.fire.connect(received.append)
    rt.qtimer.stop()
    rt.qtimer.start(20)
    _wait_ms(60)
    qapp.processEvents()
    assert received == ["A"]
    sched.acknowledge("A")
    rt.qtimer.stop()


def test_reload_from_config_adds_and_removes(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    new_cfg = _make_config(
        Reminder(name="B", color=(0, 255, 0, 255), interval_minutes=20),
    )
    sched.reload_from_config(new_cfg)
    assert "A" not in sched._timers
    assert "B" in sched._timers
    sched._timers["B"].qtimer.stop()


def test_suspend_for_sets_until_unix(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    sched.suspend_for(3600)
    info = sched.suspend_info()
    assert info.suspended is True
    assert info.until_unix is not None
    assert info.until_unix > int(time.time())
    sched._timers["A"].qtimer.stop()


def test_acknowledge_during_suspend_does_not_restart(qapp):
    cfg = _make_config(
        Reminder(name="A", color=(255, 0, 0, 255), interval_minutes=10),
    )
    sched = Scheduler(cfg)
    rt = sched._timers["A"]
    rt.in_flight = True
    sched.suspend_until_restart()
    sched.acknowledge("A")
    assert rt.paused is True
    assert not rt.qtimer.isActive()
