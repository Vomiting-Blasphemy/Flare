# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FlareReminder.

Build with:  ./build.sh
"""

from pathlib import Path

block_cipher = None

HERE = Path('.').resolve()


def _find_pyqt6_qt_dir():
    """Locate the Qt6 directory inside the active PyQt6 wheel."""
    try:
        import PyQt6
    except ImportError:
        return None
    pyqt6_dir = Path(PyQt6.__file__).resolve().parent
    qt6 = pyqt6_dir / "Qt6"
    return qt6 if qt6.is_dir() else None


def _collect_qt_wayland_plugins():
    """Bundle the Qt wayland-shell-integration plugins shipped by the
    PyQt6 wheel (xdg-shell, wl-shell, qt-shell, ivi-shell, fullscreen-shell).

    layer-shell is NOT shipped in the wheel — it must come from the
    system's layer-shell-qt package at runtime.
    """
    qt6 = _find_pyqt6_qt_dir()
    if qt6 is None:
        return []
    datas = []
    for rel_subdir in (
        "plugins/wayland-shell-integration",
        "plugins/wayland-decoration-client",
        "plugins/wayland-graphics-integration-client",
        "plugins/platforms",  # libqwayland.so
    ):
        src = qt6 / rel_subdir
        if not src.is_dir():
            continue
        dest = "PyQt6/Qt6/" + rel_subdir
        for f in src.iterdir():
            if f.is_file():
                datas.append((str(f), dest))
    return datas


a = Analysis(
    ['run_flarereminder.py'],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        ('flarereminder/resources/tray_icon.svg',           'flarereminder/resources'),
        ('flarereminder/resources/tray_icon_suspended.svg', 'flarereminder/resources'),
        *_collect_qt_wayland_plugins(),
    ],
    hiddenimports=[
        'jeepney',
        'jeepney.io',
        'jeepney.io.blocking',
        'jeepney.bus_messages',
        'jeepney.wrappers',
        'jeepney.low_level',
        'pywayland',
        'pywayland.client',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.QtSvg',
        'flarereminder',
        'flarereminder.main',
        'flarereminder.config',
        'flarereminder.stats',
        'flarereminder.flare_renderer',
        'flarereminder.overlay',
        'flarereminder.layer_shell',
        'flarereminder.scheduler',
        'flarereminder.hotkey',
        'flarereminder.tray',
        'flarereminder.settings_window',
        'flarereminder.autostart',
        'flarereminder.logging_setup',
        'flarereminder.dbus_util',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'pydoc',
        'dasbus',
        'gi',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='flarereminder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
