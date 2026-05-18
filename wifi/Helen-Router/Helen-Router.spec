# PyInstaller spec — Helen-Router single-binary build (Windows / Linux)
#
# Build: pyinstaller --noconfirm Helen-Router.spec
# Output: dist/Helen-Router/Helen-Router.exe (or ELF on Linux)

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Bundle all FastAPI / starlette / pydantic submodules so cold-start
# imports succeed inside the frozen exe.
hidden = []
for pkg in ("fastapi", "starlette", "uvicorn", "httpx",
            "websockets", "structlog", "zeroconf", "psutil"):
    hidden += collect_submodules(pkg)

datas = []
# zeroconf needs runtime data files for its mDNS implementation
datas += collect_data_files("zeroconf")

# Bundle our own ``app`` package — PyInstaller's import scanner needs
# both the path on ``pathex`` and explicit hidden imports for sub-modules.
hidden += ["app", "app.main", "app.mesh"]

a = Analysis(
    ["run.py"],
    pathex=[".", os.path.dirname(os.path.abspath(SPEC))],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy.testing",
        "pytest", "pip", "setuptools",
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
    name="Helen-Router",
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
    icon=os.path.join(os.path.dirname(os.path.abspath(SPEC)),
                      "installer-icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Helen-Router",
)
