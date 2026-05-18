# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the WINDOWED Helen-Server (pywebview + tray + embedded uvicorn).

Build from the project root:
    pyinstaller --noconfirm server_app/Helen-Server.spec

Output:
    dist/Helen-Server/Helen-Server.exe   (--onedir, windowed)

Supersedes the console-mode CommClient-Server.spec by producing a desktop
app that hosts the same FastAPI backend internally. No separate server
subprocess — uvicorn runs in a background thread inside this exe.
"""

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)
import os

block_cipher = None
SPEC_DIR = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else ".")
PROJECT_ROOT = os.path.abspath(os.path.join(SPEC_DIR, ".."))


# ── Hidden imports (same as the console build) ─────────────
hiddenimports = []
hiddenimports += collect_submodules("app")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("uvicorn.protocols")
hiddenimports += collect_submodules("uvicorn.lifespan")
hiddenimports += collect_submodules("uvicorn.loops")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_core")
hiddenimports += collect_submodules("pydantic_settings")
hiddenimports += collect_submodules("socketio")
hiddenimports += collect_submodules("engineio")
hiddenimports += collect_submodules("sqlalchemy")
hiddenimports += collect_submodules("sqlalchemy.dialects.sqlite")
hiddenimports += collect_submodules("aiosqlite")
hiddenimports += collect_submodules("alembic")
hiddenimports += collect_submodules("zeroconf")
hiddenimports += collect_submodules("structlog")
hiddenimports += collect_submodules("email_validator")
hiddenimports += collect_submodules("multipart")
# UI deps (windowed wrapper)
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("pystray")
hiddenimports += collect_submodules("PIL")
hiddenimports += [
    "compat",
    "rt_hook_compat",
    "uuid6",
    "psutil",
    "httptools",
    "httptools.parser",
    "httptools.parser.parser",
    "httptools.parser.url_parser",
    "websockets",
    "websockets.legacy",
    "websockets.legacy.server",
    "wsproto",
    "h11",
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "dotenv",
    "bcrypt",
]


# ── Data files ─────────────────────────────────────────────
datas = []
datas += collect_data_files("zeroconf")
datas += collect_data_files("email_validator")
# WebView2 + pywebview supporting assets (Edge runtime plumbing)
try:
    datas += collect_data_files("webview")
except Exception:
    pass

# .env.example + alembic.ini live at project root
for rel in (".env.example", "alembic.ini"):
    src = os.path.join(PROJECT_ROOT, rel)
    if os.path.exists(src):
        datas.append((src, "."))

# migrations/ tree (Alembic loads versions/ at runtime)
mig_dir = os.path.join(PROJECT_ROOT, "migrations")
if os.path.isdir(mig_dir):
    for root, _, files in os.walk(mig_dir):
        for fname in files:
            if fname.endswith((".py", ".mako", ".ini")):
                full = os.path.join(root, fname)
                rel_dir = os.path.relpath(root, PROJECT_ROOT)
                datas.append((full, rel_dir))

# app/transports/config/ — runtime JSON catalog
transports_config = os.path.join(PROJECT_ROOT, "app", "transports", "config")
if os.path.isdir(transports_config):
    for fname in os.listdir(transports_config):
        if fname.endswith(".json"):
            full = os.path.join(transports_config, fname)
            datas.append((full, os.path.join("app", "transports", "config")))

# The wrapper's own UI folder — loaded by pywebview at http://127.0.0.1:5175/
ui_src = os.path.join(SPEC_DIR, "ui")
if os.path.isdir(ui_src):
    for fname in os.listdir(ui_src):
        full = os.path.join(ui_src, fname)
        if os.path.isfile(full):
            datas.append((full, os.path.join("server_app", "ui")))


# ── Native libraries ───────────────────────────────────────
binaries = []
binaries += collect_dynamic_libs("cryptography")
binaries += collect_dynamic_libs("bcrypt")


a = Analysis(
    [os.path.join(SPEC_DIR, "main.py")],
    pathex=[PROJECT_ROOT, SPEC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(PROJECT_ROOT, "hooks")],
    hooksconfig={},
    runtime_hooks=[os.path.join(PROJECT_ROOT, "rt_hook_compat.py")],
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        "numpy.tests",
        "pytest",
        "_pytest",
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
    name="Helen-Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed — no console popup
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=(
        os.path.join(PROJECT_ROOT, "version_info.py")
        if os.path.exists(os.path.join(PROJECT_ROOT, "version_info.py"))
        else None
    ),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Helen-Server",
)
