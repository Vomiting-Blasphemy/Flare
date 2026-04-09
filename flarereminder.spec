# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FlareReminder.

Build with:  ./build.sh
"""

from pathlib import Path

block_cipher = None

HERE = Path('.').resolve()

a = Analysis(
    ['run_flarereminder.py'],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        ('flarereminder/resources/tray_icon.svg',           'flarereminder/resources'),
        ('flarereminder/resources/tray_icon_suspended.svg', 'flarereminder/resources'),
    ],
    hiddenimports=[
        'dasbus',
        'dasbus.connection',
        'dasbus.loop',
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
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'pydoc',
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
