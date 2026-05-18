# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for Helen-Admin (server admin desktop app).

Build (from CommClient-Server/):
    pyinstaller --noconfirm admin_app/Helen-Admin.spec

Output:
    dist/Helen-Admin/Helen-Admin.exe   (--onedir, windowed)

The admin exe embeds the admin/index.html dashboard in an Edge WebView2
window and supervises a bundled Helen-Server.exe subprocess (spawned
from server/Helen-Server.exe beside the admin exe). No external server
install is required — this is a self-contained operator tool.
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all
import os

block_cipher = None
HERE = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else ".")
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SERVER_DIST = os.path.join(PROJECT_ROOT, "dist", "Helen-Server")

# ── Hidden imports + data collection ───────────────────────
hiddenimports = []
binaries = []
extra_datas = []
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("webview.platforms")
# pywebview loads its Windows backend via pythonnet (clr)
hiddenimports += ["clr_loader", "clr_loader.ffi", "pythonnet"]
# winreg is stdlib on Windows but referenced via lazy import in the autostart
# manager — include it explicitly so PyInstaller keeps the dependency.
hiddenimports += ["winreg"]

# PIL + pystray rely on dynamic/lazy imports that PyInstaller's static
# analysis misses. collect_all pulls submodules, hidden imports, data
# files, and native binaries in one pass.
for pkg in ("PIL", "pystray"):
    _d, _b, _h = collect_all(pkg)
    extra_datas += _d
    binaries += _b
    hiddenimports += _h

# collect_all misses Pillow's native .pyd files on Windows (they live in
# the package root, not under a bin/ subdir), so we glob them in by hand.
# Without this, `from PIL import Image` crashes with:
#   ImportError: cannot import name '_imaging' from 'PIL'
import glob as _glob

_pil_root = os.path.dirname(__import__("PIL").__file__)
for _pyd in _glob.glob(os.path.join(_pil_root, "*.pyd")):
    binaries.append((_pyd, "PIL"))
# Pillow bundles libraries (zlib, libjpeg, libpng, ...) under PIL/.libs on
# some wheels; include them if present.
_pil_libs = os.path.join(_pil_root, ".libs")
if os.path.isdir(_pil_libs):
    for _dll in _glob.glob(os.path.join(_pil_libs, "*.dll")):
        binaries.append((_dll, "PIL/.libs"))

# ── Data files ──────────────────────────────────────────────
# Bundle the admin/ folder (HTML) and the Helen-Server onedir payload
# next to the admin exe so the admin exe can spawn it directly.
datas = [
    (os.path.join(PROJECT_ROOT, "admin"), "admin"),
]
if os.path.isdir(SERVER_DIST):
    # Copies the entire Helen-Server onedir tree to `server/` in the
    # admin bundle. admin_app/main.py's server_exe_path() looks at
    # `<base>/server/Helen-Server.exe` first.
    datas.append((SERVER_DIST, "server"))
else:
    # Fail early if someone builds the admin without the server. This
    # avoids silently shipping a broken bundle that can't start anything.
    raise SystemExit(
        f"Helen-Server dist not found at {SERVER_DIST}. "
        "Build the server first: pyinstaller --noconfirm CommClient-Server.spec"
    )
datas += collect_data_files("webview")
datas += extra_datas

# ── Analysis ────────────────────────────────────────────────
a = Analysis(
    [os.path.join(HERE, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
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
    name="Helen-Admin",
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
    name="Helen-Admin",
)
