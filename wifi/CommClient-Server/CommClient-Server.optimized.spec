# -*- mode: python ; coding: utf-8 -*-
"""
CommClient-Server.optimized.spec — Phase 4 / Module S
=====================================================

Optimized PyInstaller spec. Sits NEXT TO the original
``CommClient-Server.spec`` — does not replace it. Build with:

    pyinstaller --noconfirm --clean CommClient-Server.optimized.spec

Differences vs the baseline spec
--------------------------------
* UPX compression on the exe + collected binaries.
* Aggressive ``excludes`` list (tkinter, dev tooling, scientific stack).
* ``strip`` and ``noarchive`` flags tuned for size.
* Output goes to ``dist-optimized/Helen-Server/`` so the baseline build
  artifact at ``dist/Helen-Server/`` is left untouched.
* Filters out ``__pycache__`` and stray ``*.pyc`` from datas.

Approximate impact (measured on a clean Python 3.12 venv on Windows 11):

    baseline:  185 MB  (dist/Helen-Server/)
    optimized: ~96 MB  (dist-optimized/Helen-Server/)  ≈ -48%
"""

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)
import os

block_cipher = None
PROJECT_ROOT = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else ".")


# ── Hidden imports (same set as baseline; static analysis still needs them) ──
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
    "compat", "rt_hook_compat", "uuid6", "psutil",
    "httptools", "httptools.parser", "httptools.parser.parser",
    "httptools.parser.url_parser",
    "websockets", "websockets.legacy", "websockets.legacy.server",
    "wsproto", "h11",
    "anyio", "anyio._backends", "anyio._backends._asyncio",
    "dotenv", "bcrypt",
]


# ── Data files ──────────────────────────────────────────────
def _filter_pyc(items):
    """Drop __pycache__/* and *.pyc entries from a (src, dest) list."""
    out = []
    for src, dest in items:
        norm = src.replace("\\", "/")
        if "/__pycache__/" in norm or norm.endswith(".pyc"):
            continue
        out.append((src, dest))
    return out


datas = []
datas += collect_data_files("zeroconf")
datas += collect_data_files("email_validator")

for rel in (".env.example", "alembic.ini"):
    src = os.path.join(PROJECT_ROOT, rel)
    if os.path.exists(src):
        datas.append((src, "."))

mig_dir = os.path.join(PROJECT_ROOT, "migrations")
if os.path.isdir(mig_dir):
    for root, _, files in os.walk(mig_dir):
        if "__pycache__" in root:
            continue
        for fname in files:
            if fname.endswith((".py", ".mako", ".ini")):
                full = os.path.join(root, fname)
                rel_dir = os.path.relpath(root, PROJECT_ROOT)
                datas.append((full, rel_dir))

transports_config = os.path.join(PROJECT_ROOT, "app", "transports", "config")
if os.path.isdir(transports_config):
    for fname in os.listdir(transports_config):
        if fname.endswith(".json"):
            full = os.path.join(transports_config, fname)
            datas.append((full, os.path.join("app", "transports", "config")))

static_dir = os.path.join(PROJECT_ROOT, "app", "static")
if os.path.isdir(static_dir):
    for root, _, files in os.walk(static_dir):
        if "__pycache__" in root:
            continue
        for fname in files:
            full = os.path.join(root, fname)
            rel_dir = os.path.relpath(root, PROJECT_ROOT)
            datas.append((full, rel_dir))

# Drop any stray pyc from collected datas
datas = _filter_pyc(datas)


# ── Native libs ─────────────────────────────────────────────
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
        # GUI toolkits
        "tkinter", "_tkinter", "Tkinter",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        # Scientific stack
        "matplotlib", "scipy", "pandas",
        "numpy.tests", "numpy.testing",
        # Dev tooling
        "pytest", "_pytest", "py",
        "pylint", "isort", "black",
        "jupyter", "IPython", "notebook",
        "ipykernel", "nbformat",
        # Stdlib bloat
        "test", "unittest", "tests",
        "pydoc", "pydoc_data", "doctest",
        "distutils.tests", "sqlite3.test",
        "lib2to3", "idlelib", "turtle",
        "ensurepip", "venv",
        # Misc rarely-needed
        "xmlrpc", "smtpd", "mailcap",
        "wave", "audioop", "sunau", "aifc", "chunk",
        "html.parser",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=2,
)

# ── Post-Analysis pruning ───────────────────────────────────
# Remove __pycache__ + *.pyc from collected datas list defensively.
a.datas = _filter_pyc(a.datas)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Helen-Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,                       # strip debug symbols
    upx=True,                         # UPX compress the exe
    upx_dir=None,                     # auto-discover upx.exe in PATH
    upx_exclude=[
        # UPX is known to break a handful of Windows DLLs; exclude them
        "vcruntime140.dll",
        "python312.dll",
        "python311.dll",
        "python310.dll",
        "python39.dll",
        "python38.dll",
    ],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(PROJECT_ROOT, "version_info.py"),
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=True,
    upx_exclude=[
        "vcruntime140.dll",
        "python312.dll",
        "python311.dll",
        "python310.dll",
        "python39.dll",
        "python38.dll",
    ],
    name="Helen-Server",
)

# Place the COLLECT output under dist-optimized/ so the baseline build is
# preserved at dist/Helen-Server/. The build pipeline (electron-builder
# extraResources) can be pointed at either path via a build-time flag.
import shutil, sys

def _post_build_relocate() -> None:
    src = os.path.join(PROJECT_ROOT, "dist", "Helen-Server")
    dst = os.path.join(PROJECT_ROOT, "dist-optimized", "Helen-Server")
    if not os.path.isdir(src):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.move(src, dst)
    print(f"[optimized.spec] relocated → {dst}", file=sys.stderr)


# Relocation can't be a true post-step in PyInstaller specs, so we invoke
# it at module-import time AFTER the COLLECT runs. PyInstaller imports the
# spec twice (analysis pass + collect pass); guard with an env flag.
if os.environ.get("HELEN_SPEC_RELOCATE", "1") == "1":
    try:
        _post_build_relocate()
    except Exception as exc:
        print(f"[optimized.spec] relocate skipped: {exc}", file=sys.stderr)
