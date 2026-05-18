"""
PyInstaller runtime hook — installs the same compatibility shims that
compat.py provides, but BEFORE any frozen application module is imported.

This file is referenced from CommClient-Server.spec via:

    runtime_hooks=['rt_hook_compat.py']

It must be self-contained: PyInstaller copies it into the bundle and
runs it at process start, before sys.path is fully configured for the
application package, so we cannot rely on `import compat`.
"""

from __future__ import annotations

import sys
import warnings


def _install_distutils_shim() -> None:
    if "distutils" in sys.modules:
        return
    try:
        import distutils  # noqa: F401
        return
    except ImportError:
        pass
    try:
        from setuptools import _distutils as distutils_pkg  # type: ignore
        sys.modules["distutils"] = distutils_pkg
        for sub in ("util", "version", "spawn", "sysconfig", "errors", "log"):
            try:
                mod = __import__(
                    f"setuptools._distutils.{sub}", fromlist=[sub]
                )
                sys.modules[f"distutils.{sub}"] = mod
            except Exception:
                pass
    except Exception:
        pass


def _install_imp_shim() -> None:
    if "imp" in sys.modules:
        return
    try:
        import imp  # noqa: F401
        return
    except ImportError:
        pass
    import importlib
    import importlib.util
    import types

    shim = types.ModuleType("imp")
    shim.find_module = lambda name, path=None: (
        None,
        (importlib.util.find_spec(name) or (_ for _ in ()).throw(ImportError(name))).origin,
        ("", "", 0),
    )
    shim.load_module = lambda name, *a, **k: importlib.import_module(name)
    shim.new_module = lambda name: types.ModuleType(name)
    shim.acquire_lock = lambda: None
    shim.release_lock = lambda: None
    shim.PY_SOURCE = 1
    shim.PY_COMPILED = 2
    shim.C_EXTENSION = 3
    shim.PKG_DIRECTORY = 5
    sys.modules["imp"] = shim


def _silence_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r".*datetime\.datetime\.utcnow\(\) is deprecated.*",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r".*pkg_resources is deprecated.*",
        category=DeprecationWarning,
    )


def _windows_asyncio_policy() -> None:
    if sys.platform != "win32":
        return
    try:
        import asyncio
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(
                asyncio.WindowsProactorEventLoopPolicy()
            )
    except Exception:
        pass


_install_distutils_shim()
_install_imp_shim()
_silence_warnings()
_windows_asyncio_policy()
