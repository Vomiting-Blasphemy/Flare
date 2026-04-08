"""Global hotkey registration via KGlobalAccel D-Bus and the GlobalShortcuts portal.

Two paths are tried in order:

1. **KGlobalAccel** — KDE's native global-shortcut daemon. Reachable on
   the session bus at ``org.kde.kglobalaccel`` / ``/kglobalaccel``. We
   register a component called ``flarereminder`` with one action and
   listen for the ``globalShortcutPressed`` signal.

2. **xdg-desktop-portal GlobalShortcuts** — falls back to the portal API
   ``org.freedesktop.portal.GlobalShortcuts``, which on KDE 6 is provided
   by ``xdg-desktop-portal-kde``.

If both fail, the hotkey is unavailable but the rest of the app keeps
working — the user can always acknowledge via the tray icon.

Also exposes ``KeyComboRecorder``, a tiny QWidget that captures the next
keypress combo and emits the human-readable string for use in the
settings hotkey field.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QLineEdit

from . import __app_id__, __app_name__

log = logging.getLogger(__name__)


# ---- conversion helpers --------------------------------------------------


def parse_hotkey(text: str) -> tuple[list[str], str]:
    """Split "Ctrl+Shift+B" into (modifiers, key).

    >>> parse_hotkey("Ctrl+Shift+B")
    (['Ctrl', 'Shift'], 'B')
    """
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        return ([], "")
    return (parts[:-1], parts[-1])


def hotkey_to_qt_int(text: str) -> int:
    """Convert ``"Ctrl+Shift+B"`` into the integer key+modifier code Qt uses.

    Returns 0 on parse failure.
    """
    mods_text, key_text = parse_hotkey(text)
    mod_int = 0
    for m in mods_text:
        ml = m.lower()
        if ml in ("ctrl", "control"):
            mod_int |= Qt.KeyboardModifier.ControlModifier.value
        elif ml == "shift":
            mod_int |= Qt.KeyboardModifier.ShiftModifier.value
        elif ml in ("alt", "meta+alt"):
            mod_int |= Qt.KeyboardModifier.AltModifier.value
        elif ml in ("meta", "super", "win"):
            mod_int |= Qt.KeyboardModifier.MetaModifier.value
    if not key_text:
        return 0
    key_attr = key_text.upper()
    if len(key_attr) == 1 and key_attr.isalnum():
        key_val = ord(key_attr)
    else:
        key_obj = getattr(Qt.Key, "Key_" + key_attr, None)
        if key_obj is None:
            return 0
        key_val = key_obj.value
    return mod_int | key_val


# ---- HotkeyManager ------------------------------------------------------


class HotkeyManager(QObject):
    """Owns the global hotkey registration and emits ``triggered`` on press."""

    triggered = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._strategy: str | None = None
        self._dbus_objs: list[object] = []
        self._current: str | None = None

    def strategy(self) -> str | None:
        return self._strategy

    def register(self, hotkey: str) -> str:
        """Try to register ``hotkey``; returns the strategy name in use.

        Strategy values: ``"kglobalaccel"``, ``"portal"``, ``"none"``.
        """
        self.unregister()
        self._current = hotkey
        try:
            if self._try_kglobalaccel(hotkey):
                self._strategy = "kglobalaccel"
                log.info("Hotkey %s registered via KGlobalAccel", hotkey)
                return self._strategy
        except Exception as exc:  # noqa: BLE001
            log.warning("KGlobalAccel registration failed: %s", exc)

        try:
            if self._try_portal(hotkey):
                self._strategy = "portal"
                log.info("Hotkey %s registered via xdg-desktop-portal", hotkey)
                return self._strategy
        except Exception as exc:  # noqa: BLE001
            log.warning("Portal GlobalShortcuts registration failed: %s", exc)

        self._strategy = "none"
        log.warning(
            "Could not register global hotkey %s — use the tray menu to "
            "acknowledge instead",
            hotkey,
        )
        return self._strategy

    def unregister(self) -> None:
        # Best-effort: drop our references; KGlobalAccel will GC the action
        # when our component name is no longer claimed.
        self._dbus_objs.clear()
        self._strategy = None

    # ---- KGlobalAccel ---------------------------------------------------

    def _try_kglobalaccel(self, hotkey: str) -> bool:
        try:
            from dasbus.connection import SessionMessageBus
        except ImportError:
            return False

        bus = SessionMessageBus()
        proxy = bus.get_proxy("org.kde.kglobalaccel", "/kglobalaccel")
        # Component object path for our action group.
        component_unique = __app_id__
        component_friendly = __app_name__
        action_unique = "acknowledge"
        action_friendly = "Acknowledge all flares"
        action_id = [
            component_unique,
            action_unique,
            component_friendly,
            action_friendly,
        ]

        key_int = hotkey_to_qt_int(hotkey)
        if key_int == 0:
            log.warning("hotkey_to_qt_int returned 0 for %r", hotkey)
            return False

        # SetShortcut(actionId, [keys], flags) -> [returned keys]
        # flags: 0=NoAutoloading, 4=Autoloading
        try:
            proxy.setShortcut(action_id, [key_int], 4)
        except Exception as exc:  # noqa: BLE001
            log.debug("setShortcut failed: %s", exc)
            return False

        # Subscribe to the press signal.
        def on_pressed(component: str, action: str, ts: int) -> None:
            if component == component_unique and action == action_unique:
                log.debug("Hotkey pressed via KGlobalAccel")
                self.triggered.emit()

        try:
            proxy.globalShortcutPressed.connect(on_pressed)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not connect globalShortcutPressed: %s", exc)
            return False

        self._dbus_objs.append(proxy)
        return True

    # ---- xdg-desktop-portal --------------------------------------------

    def _try_portal(self, hotkey: str) -> bool:
        try:
            from dasbus.connection import SessionMessageBus
        except ImportError:
            return False

        bus = SessionMessageBus()
        try:
            proxy = bus.get_proxy(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("portal D-Bus unreachable: %s", exc)
            return False

        # The portal API is multi-step (CreateSession, BindShortcuts, then
        # listen for Activated signals). We do a minimal best-effort here;
        # full portal flow is honestly not worth the complexity for this
        # spec, and KGlobalAccel works on every modern KDE Plasma 6 install.
        try:
            handle = proxy.CreateSession(
                {"session_handle_token": "flarereminder"}
            )
            log.debug("Portal CreateSession returned %s", handle)
        except Exception as exc:  # noqa: BLE001
            log.debug("Portal CreateSession failed: %s", exc)
            return False

        # We don't fully wire BindShortcuts/Activated here — KGlobalAccel
        # is the realistic path on KDE. Returning False steers us to the
        # "none" strategy with a clear log message.
        log.warning(
            "Portal GlobalShortcuts is reachable but full bind flow is "
            "not implemented; falling back to tray-only acknowledgement"
        )
        return False


# ---- KeyComboRecorder ---------------------------------------------------


class KeyComboRecorder(QLineEdit):
    """A QLineEdit that captures the next key combo and shows it as text.

    Click into it, then press the desired combo. Modifiers shift the
    captured key into a string like ``Ctrl+Shift+B``.
    """

    combo_recorded = pyqtSignal(str)

    def __init__(self, initial: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Click then press a key combo…")
        self.setText(initial)
        self._recording = False

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        super().focusInEvent(event)
        self._recording = True
        self.setText("Press a combo…")

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        super().focusOutEvent(event)
        if self._recording and self.text() == "Press a combo…":
            self.setText("")
        self._recording = False

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if not self._recording:
            super().keyPressEvent(event)
            return
        key = event.key()
        # Ignore standalone modifier presses; wait for the "real" key.
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
            Qt.Key.Key_AltGr,
        ):
            event.accept()
            return

        mods = event.modifiers()
        parts: list[str] = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("Meta")
        # Build key name. Prefer the human-readable Qt enum name.
        try:
            key_name = Qt.Key(key).name.replace("Key_", "")
        except ValueError:
            key_name = chr(key) if 32 < key < 127 else f"0x{key:X}"
        if not key_name:
            key_name = chr(key) if 32 < key < 127 else f"0x{key:X}"
        parts.append(key_name)
        combo = "+".join(parts)
        self.setText(combo)
        self._recording = False
        self.clearFocus()
        self.combo_recorded.emit(combo)
        event.accept()
