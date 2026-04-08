"""Tests for the config module."""

from __future__ import annotations

import json
from pathlib import Path

from flarereminder.config import (
    AppConfig,
    GlobalSettings,
    Reminder,
    SuspendState,
    load_config,
    save_config,
)


def test_defaults_have_blink_and_water():
    cfg = AppConfig.defaults()
    names = [r.name for r in cfg.reminders]
    assert "Blink" in names
    assert "Drink Water" in names
    blink = cfg.find_reminder("Blink")
    assert blink is not None
    assert blink.interval_minutes == 15
    assert blink.color[3] == 255  # alpha present


def test_reminder_clamps_interval():
    r = Reminder(name="x", color=(0, 0, 0, 255), interval_minutes=0)
    assert r.interval_minutes == 1


def test_reminder_normalises_3tuple_color():
    r = Reminder(name="x", color=(10, 20, 30), interval_minutes=5)  # type: ignore[arg-type]
    assert r.color == (10, 20, 30, 255)


def test_reminder_clamps_intensity_override():
    r = Reminder(
        name="x", color=(0, 0, 0, 255), interval_minutes=5, flare_intensity_override=5.0
    )
    assert r.flare_intensity_override == 2.0
    r2 = Reminder(
        name="x", color=(0, 0, 0, 255), interval_minutes=5, flare_intensity_override=-1.0
    )
    assert r2.flare_intensity_override == 0.0


def test_round_trip_json(tmp_path: Path):
    cfg = AppConfig.defaults()
    cfg.reminders.append(
        Reminder(
            name="Stretch",
            color=(255, 100, 50, 255),
            interval_minutes=30,
            enabled=False,
            flare_intensity_override=1.5,
        )
    )
    cfg.settings.flare_intensity = 1.7
    cfg.suspend = SuspendState(suspended=True, until_unix=1_700_000_000)

    path = tmp_path / "config.json"
    save_config(cfg, path)
    assert path.exists()

    loaded = load_config(path)
    assert len(loaded.reminders) == 3
    stretch = loaded.find_reminder("Stretch")
    assert stretch is not None
    assert stretch.interval_minutes == 30
    assert stretch.enabled is False
    assert stretch.flare_intensity_override == 1.5
    assert loaded.settings.flare_intensity == 1.7
    assert loaded.suspend.suspended is True
    assert loaded.suspend.until_unix == 1_700_000_000


def test_load_missing_writes_defaults(tmp_path: Path):
    path = tmp_path / "missing.json"
    cfg = load_config(path)
    assert path.exists()
    assert cfg.find_reminder("Blink") is not None


def test_load_corrupt_returns_defaults(tmp_path: Path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.find_reminder("Blink") is not None


def test_global_settings_animation_speed_multiplier():
    s = GlobalSettings(flare_animation_speed="fast")
    assert s.animation_speed_multiplier() == 2.0
    s2 = GlobalSettings(flare_animation_speed="slow")
    assert s2.animation_speed_multiplier() == 0.5


def test_forward_compat_unknown_settings_keys(tmp_path: Path):
    path = tmp_path / "config.json"
    payload = {
        "reminders": [],
        "settings": {"flare_intensity": 1.2, "future_field": "ignored"},
        "suspend": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.settings.flare_intensity == 1.2
