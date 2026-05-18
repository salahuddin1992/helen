"""
PyInstaller runtime hook — patches `distutils` for Python 3.12+ where it
was removed from the stdlib. We re-route imports through
`setuptools._distutils`, which setuptools vendors as a drop-in replacement.

This hook is registered in CommClient-Server.spec via:
    runtime_hooks=['hooks/hook-distutils-patch.py']

It runs in the frozen application's interpreter BEFORE any user module is
imported, so any code that does `import distutils` afterwards (e.g.
through transitive third-party imports) gets the vendored copy without
needing source modifications.
"""
import sys

if sys.version_info >= (3, 12):
    try:
        import distutils  # noqa: F401
    except ImportError:
        try:
            import setuptools._distutils as distutils  # type: ignore
            sys.modules['distutils'] = distutils
            # Map common submodules so `from distutils.X import Y` keeps working
            for _sub in ("util", "version", "spawn", "sysconfig", "errors", "log"):
                try:
                    _mod = __import__(
                        f"setuptools._distutils.{_sub}", fromlist=[_sub]
                    )
                    sys.modules[f"distutils.{_sub}"] = _mod
                except Exception:
                    pass
        except ImportError:
            pass
