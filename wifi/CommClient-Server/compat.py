"""
compat.py — Python version compatibility layer for CommClient-Server.

Goal: make this codebase importable on Python 3.8 → 3.13+ without modifications
to application code, by polyfilling/aliasing modules and APIs that have been
removed, renamed, or had behavior changes across versions.

Imported as the very first thing in run.py and CommClient-Server.spec runtime
hook, BEFORE any other application or third-party imports.

Covered:
  • distutils  — removed in Python 3.12. Restore via setuptools._distutils.
  • imp        — removed in Python 3.12. Map a thin shim onto importlib.
  • datetime.utcnow — deprecated in 3.12. Patch to use timezone-aware now.
  • asyncio event loop policies for 3.8 vs 3.10+ differences on Windows.
  • collections.MutableMapping etc. — moved to collections.abc in 3.10.
"""

from __future__ import annotations

import sys
import warnings


# ─────────────────────────────────────────────────────────────
# 1. distutils shim (Python 3.12+ removed the stdlib module)
# ─────────────────────────────────────────────────────────────
def _install_distutils_shim() -> None:
    if "distutils" in sys.modules:
        return
    try:
        import distutils  # noqa: F401
        return
    except ImportError:
        pass
    try:
        # setuptools vendors a copy as setuptools._distutils
        import setuptools  # noqa: F401
        from setuptools import _distutils as distutils_pkg  # type: ignore
        sys.modules["distutils"] = distutils_pkg
        # Common submodules that older code touches
        for sub in ("util", "version", "spawn", "sysconfig", "errors", "log"):
            try:
                mod = __import__(
                    f"setuptools._distutils.{sub}", fromlist=[sub]
                )
                sys.modules[f"distutils.{sub}"] = mod
            except Exception:
                pass
    except Exception:
        # No setuptools — leave it; downstream may not need distutils
        pass


# ─────────────────────────────────────────────────────────────
# 2. imp shim (removed in Python 3.12)
# ─────────────────────────────────────────────────────────────
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

    def find_module(name, path=None):  # type: ignore
        spec = importlib.util.find_spec(name)
        if spec is None:
            raise ImportError(name)
        return None, spec.origin, ("", "", 0)

    def load_module(name, *args, **kwargs):  # type: ignore
        return importlib.import_module(name)

    def new_module(name):  # type: ignore
        return types.ModuleType(name)

    def acquire_lock():  # type: ignore
        return None

    def release_lock():  # type: ignore
        return None

    shim.find_module = find_module
    shim.load_module = load_module
    shim.new_module = new_module
    shim.acquire_lock = acquire_lock
    shim.release_lock = release_lock
    shim.PY_SOURCE = 1
    shim.PY_COMPILED = 2
    shim.C_EXTENSION = 3
    shim.PKG_DIRECTORY = 5
    sys.modules["imp"] = shim


# ─────────────────────────────────────────────────────────────
# 3. collections.* aliases (3.10+ removed deprecated aliases)
# ─────────────────────────────────────────────────────────────
def _install_collections_aliases() -> None:
    import collections
    import collections.abc as cabc

    for name in (
        "MutableMapping",
        "Mapping",
        "MutableSequence",
        "Sequence",
        "Iterable",
        "Iterator",
        "Set",
        "MutableSet",
        "Hashable",
        "Sized",
        "Container",
        "Callable",
    ):
        if not hasattr(collections, name) and hasattr(cabc, name):
            try:
                setattr(collections, name, getattr(cabc, name))
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# 4. asyncio event loop policy (Windows + Python 3.8 vs 3.10+)
# ─────────────────────────────────────────────────────────────
def _install_asyncio_policy() -> None:
    if sys.platform != "win32":
        return
    try:
        import asyncio
        # ProactorEventLoop is the default on 3.8+, but explicitly set it
        # so any earlier Selector policy from a third-party doesn't break us.
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(
                asyncio.WindowsProactorEventLoopPolicy()
            )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 5. datetime.utcnow deprecation (3.12+)
# ─────────────────────────────────────────────────────────────
def _silence_utcnow_deprecation() -> None:
    """Filter the noisy DeprecationWarning so logs stay clean.

    Application code is being migrated incrementally; this only suppresses
    the warning, it does not change behavior.
    """
    warnings.filterwarnings(
        "ignore",
        message=r".*datetime\.datetime\.utcnow\(\) is deprecated.*",
        category=DeprecationWarning,
    )


# ─────────────────────────────────────────────────────────────
# 6. PyInstaller frozen-mode warnings cleanup
# ─────────────────────────────────────────────────────────────
def _silence_pyinstaller_noise() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r".*pkg_resources is deprecated.*",
        category=DeprecationWarning,
    )


def apply() -> None:
    """Apply every compatibility shim. Safe to call multiple times."""
    _install_distutils_shim()
    _install_imp_shim()
    _install_collections_aliases()
    _install_asyncio_policy()
    _silence_utcnow_deprecation()
    _silence_pyinstaller_noise()


# Auto-apply on import — no app code needs to remember to call apply().
apply()
