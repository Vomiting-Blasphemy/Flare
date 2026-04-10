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

    # KGlobalAccel constants (see kglobalaccel/src/kglobalaccel.h).
    _KGA_NO_AUTOLOADING = 0x0   # SetShortcutFlag::NoAutoloading
    _KGA_AUTOLOADING = 0x1      # SetShortcutFlag::Autoloading

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._strategy: str | None = None
        self._listener = None                 # DBusSignalListener, if any
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
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None
        self._strategy = None

    # ---- KGlobalAccel ---------------------------------------------------

    def _try_kglobalaccel(self, hotkey: str) -> bool:
        """Register an action with KGlobalAccel over D-Bus (jeepney).

        KDE Plasma 6's KGlobalAccel exposes:

            service   org.kde.kglobalaccel
            path      /kglobalaccel
            interface org.kde.KGlobalAccel
            methods:
                doRegister(as)           — declare the action (optional, may not exist)
                setShortcut(as, ai, u)   — install [keys] with flags (also registers)
                setForeignShortcut(as, ai)  — alternate method on some KDE versions
                getComponent(s) -> o     — per-component object path
            component object:
                path      /component/<componentUnique>
                interface org.kde.kglobalaccel.Component
                signal    globalShortcutPressed(s, s, x)
        """
        from .dbus_util import DBusError, DBusSignalListener, call, open_bus

        key_int = hotkey_to_qt_int(hotkey)
        if key_int == 0:
            log.warning("hotkey_to_qt_int returned 0 for %r", hotkey)
            return False

        log.debug("KGlobalAccel: key_int for %r = 0x%08X", hotkey, key_int)

        conn = open_bus("SESSION")
        if conn is None:
            return False

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

        service = "org.kde.kglobalaccel"
        kga_path = "/kglobalaccel"
        kga_iface = "org.kde.KGlobalAccel"

        # Step 1: doRegister — this is optional and may not exist on all
        # KDE versions.  Do NOT bail out if it fails; setShortcut alone
        # also registers the action.
        try:
            call(
                conn, service, kga_path, kga_iface,
                "doRegister", "as", (action_id,),
            )
            log.debug("KGlobalAccel.doRegister succeeded")
        except DBusError as exc:
            log.debug("KGlobalAccel.doRegister failed (non-fatal, continuing): %s", exc)

        # Step 2: setShortcut — this both registers and sets the keys.
        # Try the standard method first, then fallbacks for different KDE versions.
        shortcut_set = False
        for method, sig, body in [
            ("setShortcut", "asaiu", (action_id, [key_int], self._KGA_NO_AUTOLOADING)),
            ("setForeignShortcut", "asai", (action_id, [key_int])),
        ]:
            try:
                result = call(
                    conn, service, kga_path, kga_iface,
                    method, sig, body,
                )
                log.info("KGlobalAccel.%s succeeded (result=%s)", method, result)
                shortcut_set = True
                break
            except DBusError as exc:
                log.debug("KGlobalAccel.%s failed: %s", method, exc)

        if not shortcut_set:
            log.warning("All KGlobalAccel shortcut registration methods failed")
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        # Step 3: getComponent(componentUnique) -> object path.
        # Try to get the exact component path for signal matching.
        component_path: str | None = None
        try:
            body = call(
                conn, service, kga_path, kga_iface,
                "getComponent", "s", (component_unique,),
            )
            if body:
                component_path = str(body[0])
                log.debug("KGlobalAccel component path: %s", component_path)
        except DBusError as exc:
            log.debug(
                "KGlobalAccel.getComponent failed: %s (will match on interface only)",
                exc,
            )

        # The probe conn is not used for signal listening; close it. The
        # DBusSignalListener below opens its own connection.
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

        # Step 4: listen for globalShortcutPressed signals.
        # Try subscribing on the component path first, then on any path.
        listener = DBusSignalListener("SESSION")
        if not listener.start():
            log.warning("Could not open session bus for KGlobalAccel listener")
            return False

        def on_pressed(*body) -> None:
            # Signal body: (componentUnique: str, shortcutUnique: str, ts: int)
            log.debug("KGlobalAccel signal received: body=%s", body)
            if len(body) < 2:
                return
            component = str(body[0])
            action = str(body[1])
            if component == component_unique and action == action_unique:
                log.info("Hotkey pressed via KGlobalAccel")
                self.triggered.emit()

        # Try subscribing with the component path first, then without.
        ok = False
        if component_path:
            ok = listener.subscribe(
                path=component_path,
                interface="org.kde.kglobalaccel.Component",
                member="globalShortcutPressed",
                callback=on_pressed,
            )
            if ok:
                log.debug("Subscribed to globalShortcutPressed on path %s", component_path)
        if not ok:
            # Fallback: match on interface+member only (any path).
            ok = listener.subscribe(
                interface="org.kde.kglobalaccel.Component",
                member="globalShortcutPressed",
                callback=on_pressed,
            )
            if ok:
                log.debug("Subscribed to globalShortcutPressed (any path)")

        if not ok:
            log.warning("Could not subscribe to globalShortcutPressed signal")
            listener.stop()
            return False

        self._listener = listener
        return True

    # ---- xdg-desktop-portal --------------------------------------------

    def _try_portal(self, hotkey: str) -> bool:
        """Portal GlobalShortcuts fallback probe.

        The full portal flow requires CreateSession, BindShortcuts, and an
        Activated signal handler — in practice KGlobalAccel covers every
        modern KDE Plasma 6 install, so we just probe the portal's
        existence and log a clear note.
        """
        from .dbus_util import DBusError, call, open_bus

        conn = open_bus("SESSION")
        if conn is None:
            return False
        try:
            # Ping the portal's root object. If it doesn't exist the call
            # returns an UnknownService / UnknownObject error.
            call(
                conn,
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.DBus.Peer",
                "Ping",
            )
            log.warning(
                "xdg-desktop-portal is reachable but the full GlobalShortcuts "
                "bind flow is intentionally not implemented. "
                "Use the tray menu to acknowledge flares or configure a "
                "shortcut manually in KDE System Settings."
            )
        except DBusError as exc:
            log.debug("portal Ping failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
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
