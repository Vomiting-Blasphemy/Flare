"""Configuration model and JSON persistence for FlareReminder.

All user configuration lives in ``~/.config/flarereminder/config.json``.
On first launch the file is created with sensible defaults (Blink at 15
minutes, Drink Water at 45 minutes, hotkey Ctrl+Shift+B).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.path.expanduser("~/.config/flarereminder"))
CONFIG_FILE = CONFIG_DIR / "config.json"

# RGBA tuples in 0-255.
WHITE: tuple[int, int, int, int] = (255, 255, 255, 255)
CORNFLOWER_BLUE: tuple[int, int, int, int] = (100, 149, 237, 255)


@dataclass
class Reminder:
    """A single user-defined reminder."""

    name: str
    color: tuple[int, int, int, int]
    interval_minutes: int
    enabled: bool = True
    flare_intensity_override: float | None = None  # None == use global
    flare_transparency: float | None = None  # None == use global; 0.0=invisible, 1.0=opaque

    def __post_init__(self) -> None:
        if self.interval_minutes < 1:
            self.interval_minutes = 1
        # Color may come back from JSON as a list; normalise.
        self.color = tuple(int(c) for c in self.color)  # type: ignore[assignment]
        if len(self.color) == 3:
            self.color = (*self.color, 255)  # type: ignore[assignment]
        if (
            self.flare_intensity_override is not None
            and not 0.0 <= self.flare_intensity_override <= 2.0
        ):
            self.flare_intensity_override = max(
                0.0, min(2.0, self.flare_intensity_override)
            )
        if (
            self.flare_transparency is not None
            and not 0.0 <= self.flare_transparency <= 1.0
        ):
            self.flare_transparency = max(0.0, min(1.0, self.flare_transparency))


@dataclass
class GlobalSettings:
    """Application-wide options."""

    flare_intensity: float = 1.0  # 0.1 .. 2.0
    flare_animation_speed: str = "normal"  # "slow" | "normal" | "fast"
    idle_timeout_seconds: int = 60
    launch_at_startup: bool = False
    hotkey: str = "Ctrl+Shift+B"
    # Per-flare visual defaults — also configurable per reminder.
    glow_radius: int = 360
    streak_length: int = 1100
    streak_count: int = 5
    secondary_count: int = 3
    pulse_intensity: float = 0.10  # 10%
    flare_transparency: float = 1.0  # 0.0=invisible, 1.0=fully opaque

    def animation_speed_multiplier(self) -> float:
        return {"slow": 0.5, "normal": 1.0, "fast": 2.0}.get(
            self.flare_animation_speed, 1.0
        )


@dataclass
class SuspendState:
    """Persisted suspend state from the tray menu."""

    suspended: bool = False
    until_unix: int | None = None  # None + suspended==True means until restart


@dataclass
class AppConfig:
    """Top-level config object — what gets serialised to JSON."""

    reminders: list[Reminder] = field(default_factory=list)
    settings: GlobalSettings = field(default_factory=GlobalSettings)
    suspend: SuspendState = field(default_factory=SuspendState)

    # ---- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminders": [asdict(r) for r in self.reminders],
            "settings": asdict(self.settings),
            "suspend": asdict(self.suspend),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        reminders = [Reminder(**r) for r in data.get("reminders", [])]
        settings_data = data.get("settings", {})
        # Forward-compat: ignore unknown keys.
        valid_settings = {
            k: v for k, v in settings_data.items() if k in GlobalSettings.__dataclass_fields__
        }
        settings = GlobalSettings(**valid_settings)
        suspend_data = data.get("suspend", {})
        valid_suspend = {
            k: v for k, v in suspend_data.items() if k in SuspendState.__dataclass_fields__
        }
        suspend = SuspendState(**valid_suspend)
        return cls(reminders=reminders, settings=settings, suspend=suspend)

    # ---- defaults ---------------------------------------------------------

    @classmethod
    def defaults(cls) -> "AppConfig":
        return cls(
            reminders=[
                Reminder(name="Blink", color=WHITE, interval_minutes=15),
                Reminder(
                    name="Drink Water",
                    color=CORNFLOWER_BLUE,
                    interval_minutes=45,
                ),
            ],
            settings=GlobalSettings(),
            suspend=SuspendState(),
        )

    # ---- helpers ----------------------------------------------------------

    def find_reminder(self, name: str) -> Reminder | None:
        for r in self.reminders:
            if r.name == name:
                return r
        return None


# ---- module-level load/save ----------------------------------------------


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    """Load config from disk; write defaults if missing or unreadable."""
    try:
        if not path.exists():
            log.info("No config file at %s, writing defaults", path)
            cfg = AppConfig.defaults()
            save_config(cfg, path)
            return cfg
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = AppConfig.from_dict(data)
        log.debug("Loaded config from %s (%d reminders)", path, len(cfg.reminders))
        return cfg
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("Failed to load config (%s); using defaults", exc)
        return AppConfig.defaults()


def save_config(cfg: AppConfig, path: Path = CONFIG_FILE) -> None:
    """Atomically write the config to disk."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, indent=2, sort_keys=True)
        tmp.replace(path)
        log.debug("Wrote config to %s", path)
    except OSError as exc:
        log.error("Failed to save config to %s: %s", path, exc)
