# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for CommClient-Admin (server admin desktop app).

Build (from CommClient-Server/):
    pyinstaller --noconfirm admin_app/CommClient-Admin.spec

Output:
    dist/CommClient-Admin/CommClient-Admin.exe   (--onedir, windowed)

The admin exe embeds the admin/index.html dashboard and opens it in a
native Edge WebView2 window. Requires the separate CommClient-Server.exe
to be running (listens on http://localhost:3000).
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files
import os

block_cipher = None
HERE = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else ".")
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))

# ── Hidden imports ──────────────────────────────────────────
hiddenimports = []
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("webview.platforms")
# pywebview loads its Windows backend via pythonnet (clr)
hiddenimports += ["clr_loader", "clr_loader.ffi", "pythonnet"]

# ── Data files ──────────────────────────────────────────────
# Bundle the admin/ folder (HTML) next to the exe via PyInstaller datas.
datas = [
    (os.path.join(PROJECT_ROOT, "admin"), "admin"),
]
datas += collect_data_files("webview")

# ── Analysis ────────────────────────────────────────────────
a = Analysis(
    [os.path.join(HERE, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep size small — exclude things we never use
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
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
    name="CommClient-Admin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app, no console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CommClient-Admin",
)
