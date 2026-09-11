# -*- mode: python ; coding: utf-8 -*-
#
# CATXT.spec — PyInstaller build spec for CATXT Sync tray app
#
# Build with:  pyinstaller CATXT.spec --noconfirm
# Output:      dist/CATXT/CATXT.exe  (--onedir)

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None
HERE = Path(SPECPATH)

# Collect playwright's driver binaries and package data (the node.js
# automation driver lives inside the Python package under playwright/driver/).
playwright_datas = collect_data_files("playwright", include_py_files=True)

a = Analysis(
    [str(HERE / "catxt_app.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        # Include config.json as a default template (copied next to exe at runtime)
        (str(HERE / "config.json"),           "."),
        # Assets (icon)
        (str(HERE / "assets"),                "assets"),
        # Playwright Python package data (browser driver paths etc.)
        *playwright_datas,
    ],
    hiddenimports=[
        # pystray backends
        "pystray._win32",
        # plyer notification backend
        "plyer.platforms.win.notification",
        # Pillow image formats used by pystray
        "PIL._imaging",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        "PIL.ImageEnhance",
        # tkinter
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "tkinter.simpledialog",
        # playwright internals
        "playwright.sync_api",
        # requests / urllib3
        "requests",
        "urllib3",
        "charset_normalizer",
        "certifi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "numpy", "pandas", "scipy",
        "IPython", "jupyter",
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
    [],
    exclude_binaries=True,
    name="CATXT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # no console window — tray app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / "assets" / "catxt.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CATXT",
)
