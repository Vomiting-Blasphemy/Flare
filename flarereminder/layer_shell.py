"""KDE Plasma Wayland overlay-stacking helpers.

PyQt6 doesn't natively expose the ``zwlr_layer_shell_v1`` protocol — its
QPA always creates an ``xdg_toplevel`` for top-level windows. This module
implements a best-effort hybrid:

1. **LayerShellQt** (preferred): if KDE's ``layer-shell-qt`` is installed
   the QPA plugin can be loaded via the ``QT_WAYLAND_SHELL_INTEGRATION``
   environment variable *before* QApplication is constructed. We expose
   ``maybe_enable_layer_shell_integration()`` for this purpose, called
   from ``main.py`` ahead of QApplication. After QApplication is up we
   try the ctypes interface to actually mark the window as
   ``Layer::Overlay`` with anchors and exclusive zone.
2. **KWin scripting fallback**: if (1) isn't available we install a
   one-shot KWin JavaScript that finds windows by ``windowClass`` and
   forces ``keepAbove``, ``skipTaskbar``, ``skipPager``, ``noBorder``.
3. **Final fallback**: trust ``Qt::WindowStaysOnTopHint`` only.

All branches are wrapped in ``try/except`` and logged. The function
returns the strategy that was actually applied so the caller can log it.
"""

from __future__ import annotations

import logging
import os
import shutil
import textwrap
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Window class string we set on the overlay so KWin can find it.
OVERLAY_WINDOW_CLASS = "flarereminder-overlay"

# zwlr_layer_shell_v1 layer enum values.
LAYER_BACKGROUND = 0
LAYER_BOTTOM = 1
LAYER_TOP = 2
LAYER_OVERLAY = 3

# Anchor bitmask.
ANCHOR_TOP = 1
ANCHOR_BOTTOM = 2
ANCHOR_LEFT = 4
ANCHOR_RIGHT = 8
ANCHOR_ALL = ANCHOR_TOP | ANCHOR_BOTTOM | ANCHOR_LEFT | ANCHOR_RIGHT

# Keyboard interactivity.
KBD_NONE = 0
KBD_EXCLUSIVE = 1
KBD_ON_DEMAND = 2


# Common system locations for Qt 6 wayland-shell-integration plugins.
# These are the paths Arch's layer-shell-qt package installs to.
_SYSTEM_QT_PLUGIN_DIRS = (
    "/usr/lib/qt6/plugins",
    "/usr/lib64/qt6/plugins",
    "/usr/lib/x86_64-linux-gnu/qt6/plugins",
)

# The filename Qt looks for when QT_WAYLAND_SHELL_INTEGRATION=layer-shell.
_LAYER_SHELL_PLUGIN_BASENAMES = (
    "libqwayland-layer-shell-integration.so",
    "libqwayland-layer-shell.so",
)


def _find_system_layer_shell_plugin() -> str | None:
    """Return the directory of the system layer-shell Qt plugin, or None."""
    for base in _SYSTEM_QT_PLUGIN_DIRS:
        wsi = Path(base) / "wayland-shell-integration"
        if not wsi.is_dir():
            continue
        for candidate in _LAYER_SHELL_PLUGIN_BASENAMES:
            if (wsi / candidate).is_file():
                return str(base)
    return None


def maybe_enable_layer_shell_integration() -> bool:
    """Hint Qt to use the layer-shell-qt QPA plugin, if available.

    Must be called BEFORE constructing ``QApplication``. Returns True iff
    the env var was set and Qt should actually be able to load the plugin.

    Handles both the "run from source" case (system PyQt6 picks up the
    system plugin via the system plugin path) and the "frozen binary"
    case (we must append the system plugin path to ``QT_PLUGIN_PATH`` so
    Qt finds the layer-shell plugin alongside the bundled plugins).
    """
    if os.environ.get("QT_WAYLAND_SHELL_INTEGRATION"):
        log.debug("QT_WAYLAND_SHELL_INTEGRATION already set; honoring it")
        return True  # respect user override

    system_plugin_dir = _find_system_layer_shell_plugin()
    if system_plugin_dir is None:
        log.debug(
            "System layer-shell-qt plugin not found; running with default "
            "Qt Wayland integration. Install the `layer-shell-qt` package "
            "to get fullscreen-game stacking."
        )
        return False

    # Append the system Qt plugin path so Qt can actually load the
    # layer-shell plugin when we're a PyInstaller frozen binary.
    existing = os.environ.get("QT_PLUGIN_PATH", "")
    if system_plugin_dir not in existing.split(os.pathsep):
        os.environ["QT_PLUGIN_PATH"] = (
            system_plugin_dir + (os.pathsep + existing if existing else "")
        )
        log.info("Prepended %s to QT_PLUGIN_PATH", system_plugin_dir)

    os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
    log.info(
        "Enabled QT_WAYLAND_SHELL_INTEGRATION=layer-shell (system plugin at %s)",
        system_plugin_dir,
    )
    return True


def apply_overlay_layer(qwindow: Any) -> str:
    """Try to promote ``qwindow`` to a wlr-layer-shell overlay surface.

    Returns one of:
        - ``"layer-shell"``       — LayerShellQt path succeeded
        - ``"kwin-script"``       — KWin scripting fallback succeeded
        - ``"keep-above-only"``   — only Qt's stays-on-top hint is in effect

    Never raises.
    """
    # Strategy 1: LayerShellQt via ctypes.
    try:
        if _try_layer_shell_qt(qwindow):
            log.info("Overlay stacking strategy: layer-shell-qt")
            return "layer-shell"
    except Exception as exc:  # noqa: BLE001
        log.warning("LayerShellQt path failed: %s", exc)

    # Strategy 2: KWin scripting via D-Bus.
    try:
        if _try_kwin_keep_above_script():
            log.info("Overlay stacking strategy: kwin-script")
            return "kwin-script"
    except Exception as exc:  # noqa: BLE001
        log.warning("KWin scripting path failed: %s", exc)

    log.warning(
        "No layer-shell or KWin scripting available; relying on "
        "Qt::WindowStaysOnTopHint only. Fullscreen apps may cover the overlay."
    )
    return "keep-above-only"


# ---- LayerShellQt -------------------------------------------------------


def _try_layer_shell_qt(qwindow: Any) -> bool:
    """Best-effort attempt to mark ``qwindow`` as a layer-shell overlay.

    The layer-shell-qt QPA plugin is loaded at QApplication construction
    time by Qt itself (triggered by ``QT_WAYLAND_SHELL_INTEGRATION=layer-shell``
    plus a reachable plugin file). Once loaded it reads the following
    QSurface properties at surface-creation time:

        layershell.layer                 -> 0..3 (Overlay = 3)
        layershell.anchors               -> edge bitmask
        layershell.exclusiveZone         -> int32 (-1 = ignore)
        layershell.keyboardInteractivity -> 0..2 (None = 0)
        layershell.scope                 -> string namespace

    So this function just sets those properties. Whether Qt *actually*
    produced a layer-shell surface depends on whether the QPA plugin was
    loaded — we treat that as "armed" if the env var is set.
    """
    if os.environ.get("QT_WAYLAND_SHELL_INTEGRATION") != "layer-shell":
        return False
    try:
        qwindow.setProperty("layershell.layer", LAYER_OVERLAY)
        qwindow.setProperty("layershell.anchors", ANCHOR_ALL)
        qwindow.setProperty("layershell.exclusiveZone", -1)
        qwindow.setProperty("layershell.keyboardInteractivity", KBD_NONE)
        qwindow.setProperty("layershell.scope", OVERLAY_WINDOW_CLASS)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not set layershell.* QSurface properties: %s", exc)
        return False
    return True


# ---- KWin scripting fallback --------------------------------------------


_KWIN_SCRIPT_TEMPLATE = textwrap.dedent(
    """
    // FlareReminder overlay keep-above script.
    // Finds windows whose resourceClass matches our app id and forces them
    // to be kept above all others, skipped from taskbar/pager, borderless.
    function fixOverlay(client) {
        if (!client) return;
        try {
            var cls = (client.resourceClass || "").toString();
            if (cls.indexOf("__APP_ID__") !== -1) {
                client.keepAbove = true;
                client.skipTaskbar = true;
                client.skipPager = true;
                client.skipSwitcher = true;
                client.noBorder = true;
                client.onAllDesktops = true;
            }
        } catch (e) {}
    }
    workspace.clientAdded.connect(fixOverlay);
    // Existing windows.
    var clients = workspace.clientList ? workspace.clientList() : workspace.windowList();
    for (var i = 0; i < clients.length; i++) fixOverlay(clients[i]);
    """
).strip()


def _try_kwin_keep_above_script() -> bool:
    """Install a one-shot KWin script via D-Bus (jeepney).

    Returns True if the script was loaded successfully.
    """
    from .dbus_util import DBusError, call, open_bus

    script_path = _write_kwin_script()
    if script_path is None:
        return False

    conn = open_bus("SESSION")
    if conn is None:
        return False

    service = "org.kde.KWin"
    scripting_path = "/Scripting"
    scripting_iface = "org.kde.kwin.Scripting"

    try:
        # loadScript(filePath, pluginName) -> int scriptId
        body = call(
            conn, service, scripting_path, scripting_iface,
            "loadScript", "ss", (str(script_path), "flarereminder-overlay"),
        )
        if not body:
            log.debug("KWin loadScript returned no body")
            return False
        script_id = int(body[0])
        # KWin 5/6: run the script on its sub-object.
        sub_path = f"/Scripting/Script{script_id}"
        ran = False
        for iface in ("org.kde.kwin.Script", "org.kde.kwin.Scripting"):
            try:
                call(conn, service, sub_path, iface, "run")
                ran = True
                break
            except DBusError:
                continue
        if not ran:
            # Older KWin: call start() on the root scripting object.
            try:
                call(conn, service, scripting_path, scripting_iface, "start")
                ran = True
            except DBusError as exc:
                log.debug("KWin script start() also failed: %s", exc)
        return ran
    except DBusError as exc:
        log.debug("KWin scripting D-Bus call failed: %s", exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _write_kwin_script() -> str | None:
    """Write the KWin JS to a tempfile and return its path."""
    try:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        script_dir = os.path.join(runtime_dir, "flarereminder")
        os.makedirs(script_dir, exist_ok=True)
        path = os.path.join(script_dir, "overlay-keep-above.js")
        contents = _KWIN_SCRIPT_TEMPLATE.replace("__APP_ID__", OVERLAY_WINDOW_CLASS)
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
        return path
    except OSError as exc:
        log.debug("Could not write KWin script: %s", exc)
        return None


# ---- diagnostic --------------------------------------------------------


def have_kwin_dbus() -> bool:
    """Cheap test for whether KWin's D-Bus service is reachable."""
    from .dbus_util import DBusError, call, open_bus

    conn = open_bus("SESSION")
    if conn is None:
        return False
    try:
        body = call(
            conn,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListNames",
        )
        return bool(body) and "org.kde.KWin" in body[0]
    except DBusError:
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def kwin_helper_available() -> bool:
    """Whether ``kwin_wayland`` or ``kwin_x11`` is on PATH."""
    return shutil.which("kwin_wayland") is not None or shutil.which("kwin_x11") is not None
