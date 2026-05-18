# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for CommClient-Server (LAN/WiFi-only FastAPI server).

Build:
    pyinstaller --noconfirm CommClient-Server.spec

Output:
    dist/CommClient-Server/CommClient-Server.exe   (--onedir)

The desktop project (../CommClient-Desktop/electron-builder.yml) reads from
this exact path via `extraResources`.
"""

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)
import os

block_cipher = None
PROJECT_ROOT = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else ".")


# ── Hidden imports ──────────────────────────────────────────
# PyInstaller's static analysis misses dynamically-imported modules. List
# every package whose submodules are loaded by string name (FastAPI route
# discovery, SQLAlchemy dialect lookup, uvicorn protocol auto-detect, etc.)
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


# ── Data files (non-Python resources bundled into the exe) ──
datas = []
datas += collect_data_files("zeroconf")
datas += collect_data_files("email_validator")
# Project resources used at runtime
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

# app/transports/config/ — JSON catalog (1169 transports) + detection rules.
# TransportRegistry loads these at runtime via __file__-relative paths, so
# PyInstaller must ship them next to the frozen module.
transports_config = os.path.join(PROJECT_ROOT, "app", "transports", "config")
if os.path.isdir(transports_config):
    for fname in os.listdir(transports_config):
        if fname.endswith(".json"):
            full = os.path.join(transports_config, fname)
            datas.append((full, os.path.join("app", "transports", "config")))

# app/static/ — phone pair HTML, any other static assets served directly by
# FastAPI. The pair flow loads static/pair.html via a __file__-relative
# Path(); without these entries the frozen exe returns 500 "Pair page
# missing" on /pair?t=... requests from scanned QR codes.
static_dir = os.path.join(PROJECT_ROOT, "app", "static")
if os.path.isdir(static_dir):
    for root, _, files in os.walk(static_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel_dir = os.path.relpath(root, PROJECT_ROOT)
            datas.append((full, rel_dir))

# iOS/web-simulator/ and iOS-Admin/web-simulator/ — iPhone-sized web
# clients served at /mobile/ and /admin-mobile/ by the running server.
# Kept at the repo root so the same source feeds both dev (python
# run.py) and the frozen bundle. Bundle layout preserves the parent
# folder names so app.main can find them via sys._MEIPASS / <name> /
# web-simulator.
for _sim_parent in ("iOS", "iOS-Admin"):
    _sim_src = os.path.abspath(
        os.path.join(PROJECT_ROOT, "..", _sim_parent, "web-simulator")
    )
    if not os.path.isdir(_sim_src):
        continue
    for root, _, files in os.walk(_sim_src):
        for fname in files:
            full = os.path.join(root, fname)
            rel_dir = os.path.relpath(root, _sim_src)
            dest_dir = os.path.join(_sim_parent, "web-simulator", rel_dir) \
                if rel_dir != "." \
                else os.path.join(_sim_parent, "web-simulator")
            datas.append((full, dest_dir))

# sfu-worker/ — Node.js mediasoup SFU worker source (no node_modules).
# sfu_launcher.py looks for sfu-worker via _MEIPASS, then runs
# `npm install` lazily on first SFU promotion (skipped via env). We
# bundle:
#   - src/             (~10 KB)
#   - package.json     (declarative deps)
#   - package-lock.json (deterministic install)
#   - README.md
# We DELIBERATELY do NOT bundle node_modules (42 MB +
# platform-specific mediasoup binary). Operators need Node 18+ on the
# host. See docs/deployment-mediasoup.md.
_sfu_worker = os.path.join(PROJECT_ROOT, "sfu-worker")
if os.path.isdir(_sfu_worker):
    _SFU_BUNDLE_TOP = {"package.json", "package-lock.json", "README.md"}
    for root, dirs, files in os.walk(_sfu_worker):
        # Skip node_modules and any nested .git, .cache, etc.
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", ".cache", "test"}]
        for fname in files:
            rel_dir = os.path.relpath(root, _sfu_worker)
            # Top-level: only the explicit allowlist.
            if rel_dir == "." and fname not in _SFU_BUNDLE_TOP:
                continue
            full = os.path.join(root, fname)
            dest = os.path.join("sfu-worker", rel_dir) if rel_dir != "." else "sfu-worker"
            datas.append((full, dest))


# ── Native libraries (cffi/cryptography backends) ───────────
binaries = []
binaries += collect_dynamic_libs("cryptography")
binaries += collect_dynamic_libs("bcrypt")


a = Analysis(
    ["run.py"],
    pathex=[PROJECT_ROOT],
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
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(PROJECT_ROOT, "version_info.py"),
    # Run as the invoking user. Helen-Server only binds high ports
    # (3000 / 3443 / 41234 — all > 1024) so admin is not required for
    # the main service. Firewall rule provisioning still needs admin
    # but the installer does that ONCE at install time, not at every
    # launch. Forcing UAC per launch added 3-5 s + a popup the user
    # has to dismiss — gone now.
    uac_admin=False,
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
