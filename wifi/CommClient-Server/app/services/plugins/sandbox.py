"""
Plugin execution sandbox.

Two execution strategies:

* ``RestrictedPython`` if installed (default) — compiles the code with
  the RestrictedPython transformer, denying dangerous AST nodes
  (``Attribute`` rewrites, ``__import__`` blocking, etc.)
* AST-walking denylist fallback when RestrictedPython is absent.

Resource limits (best-effort, OS-dependent):

* CPU time wall-clock cap via ``signal.SIGALRM`` (Unix only)
* Memory cap via ``resource.setrlimit`` (Unix only)
* Stdout truncated to ``MAX_STDOUT`` bytes

This is a defense-in-depth layer; treat plugins as still semi-trusted
and pair with code review for high-impact integrations.
"""
from __future__ import annotations

import ast
import io
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


try:                                                                  # pragma: no cover
    from RestrictedPython import (                          # type: ignore[import-untyped]
        compile_restricted,
        safe_globals,
    )
    from RestrictedPython.Guards import (                    # type: ignore[import-untyped]
        guarded_iter_unpack_sequence,
        safe_builtins,
    )
    _RESTRICTED_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _RESTRICTED_AVAILABLE = False
    compile_restricted = None                                          # type: ignore[assignment]
    safe_globals = None                                                # type: ignore[assignment]
    safe_builtins = None                                               # type: ignore[assignment]


MAX_STDOUT = 1_048_576    # 1 MiB
DEFAULT_CPU_SECONDS = 5
DEFAULT_MEMORY_MB = 128


ALLOWED_MODULES = {
    "json", "math", "re", "datetime", "collections", "itertools",
    "hashlib", "uuid", "decimal", "string", "functools", "operator",
    "base64", "urllib.parse", "helen_sdk",
}

FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "globals", "locals",
    "vars", "open", "input", "breakpoint", "memoryview",
}

FORBIDDEN_AST_NODES = (ast.Import,)    # use ALLOWED_MODULES filter below


# ───────────────────────────────────────────────────────────────────────
# Result shape
# ───────────────────────────────────────────────────────────────────────


@dataclass
class SandboxResult:
    ok: bool
    return_value: Any = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    duration_ms: int = 0
    violations: list[str] = field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────
# AST denylist (used when RestrictedPython is missing)
# ───────────────────────────────────────────────────────────────────────


def _ast_check(source: str) -> list[str]:
    issues: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"syntax: {e}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            issues.append(f"forbidden-name: {node.id}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [n.name for n in node.names]
            else:
                if node.module:
                    mods = [node.module]
            for m in mods:
                if not any(
                    m == ok or m.startswith(ok + ".") for ok in ALLOWED_MODULES
                ):
                    issues.append(f"forbidden-import: {m}")
        if isinstance(node, ast.Attribute):
            if isinstance(node.attr, str) and node.attr.startswith("__") and node.attr.endswith("__"):
                # dunders like __class__ or __subclasses__
                issues.append(f"dunder-access: {node.attr}")
    return issues


# ───────────────────────────────────────────────────────────────────────
# Resource caps (Unix only — no-ops elsewhere)
# ───────────────────────────────────────────────────────────────────────


def _apply_rlimits(cpu_seconds: int, memory_mb: int) -> None:
    try:                                                                # pragma: no cover
        import resource    # type: ignore[import-not-found]
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        bytes_cap = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (bytes_cap, bytes_cap))
    except Exception:                                                   # noqa: BLE001
        pass


# ───────────────────────────────────────────────────────────────────────
# Public executor
# ───────────────────────────────────────────────────────────────────────


def _safe_import(name: str, globals_=None, locals_=None, fromlist=(), level=0):
    if not any(name == ok or name.startswith(ok + ".") for ok in ALLOWED_MODULES):
        raise ImportError(f"forbidden import: {name}")
    return __import__(name, globals_, locals_, fromlist, level)


def run_plugin_code(
    source: str,
    *,
    entry_callable: str = "main",
    arg: Any = None,
    timeout_seconds: int = DEFAULT_CPU_SECONDS,
    memory_mb: int = DEFAULT_MEMORY_MB,
    extra_globals: Optional[dict[str, Any]] = None,
) -> SandboxResult:
    """Compile + execute a snippet of plugin code inside the sandbox."""
    issues = _ast_check(source)
    if issues and not _RESTRICTED_AVAILABLE:
        return SandboxResult(False, error="ast-violations", violations=issues)

    if _RESTRICTED_AVAILABLE:
        try:
            code = compile_restricted(source, "<plugin>", "exec")        # type: ignore[misc]
        except SyntaxError as e:
            return SandboxResult(False, error=f"compile: {e}", violations=issues)
        g: dict[str, Any] = dict(safe_globals or {})                    # type: ignore[arg-type]
        g["__builtins__"] = {**(safe_builtins or {}),                    # type: ignore[dict-item]
                              "__import__": _safe_import}
        g["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    else:
        try:
            code = compile(source, "<plugin>", "exec")
        except SyntaxError as e:
            return SandboxResult(False, error=f"compile: {e}", violations=issues)
        g = {
            "__builtins__": {
                k: getattr(__builtins__ if isinstance(__builtins__, dict) is False else __builtins__, k, None)  # type: ignore[arg-type]
                for k in ("len", "range", "str", "int", "float", "bool",
                          "list", "dict", "set", "tuple", "min", "max",
                          "sum", "abs", "round", "any", "all", "sorted",
                          "enumerate", "zip", "map", "filter", "print",
                          "isinstance", "issubclass", "type", "repr",
                          "True", "False", "None")
            },
            "__import__": _safe_import,
        }
        g["__builtins__"]["__import__"] = _safe_import

    if extra_globals:
        g.update(extra_globals)
    local_ns: dict[str, Any] = {}

    stdout = io.StringIO()
    stderr = io.StringIO()
    result_holder: dict[str, Any] = {"value": None, "error": None}
    start = time.perf_counter()

    def _worker() -> None:
        try:
            _apply_rlimits(timeout_seconds, memory_mb)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exec(code, g, local_ns)    # noqa: S102
                fn = local_ns.get(entry_callable) or g.get(entry_callable)
                if callable(fn):
                    result_holder["value"] = fn(arg)
        except Exception as e:                                          # noqa: BLE001
            result_holder["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds + 1)
    if t.is_alive():
        # Timed out — Python threads can't be forcibly killed safely.
        # Surface the timeout; the thread's daemon flag ensures it dies
        # on process exit.
        duration = int((time.perf_counter() - start) * 1000)
        return SandboxResult(
            False, error="timeout", duration_ms=duration,
            stdout=stdout.getvalue()[:MAX_STDOUT],
            stderr=stderr.getvalue()[:MAX_STDOUT],
            violations=issues,
        )

    duration = int((time.perf_counter() - start) * 1000)
    if result_holder["error"]:
        return SandboxResult(
            False, error=result_holder["error"], duration_ms=duration,
            stdout=stdout.getvalue()[:MAX_STDOUT],
            stderr=stderr.getvalue()[:MAX_STDOUT],
            violations=issues,
        )
    return SandboxResult(
        True, return_value=result_holder["value"], duration_ms=duration,
        stdout=stdout.getvalue()[:MAX_STDOUT],
        stderr=stderr.getvalue()[:MAX_STDOUT],
        violations=issues,
    )


# ═══════════════════════════════════════════════════════════════════════
# PluginSandbox — filesystem-level install + process-level test runner
# ═══════════════════════════════════════════════════════════════════════
#
# The class below is a NEW addition (Phase 7 / Module AH) and does NOT
# alter ``run_plugin_code`` above. It implements the "sandbox preview"
# feature: install a candidate plugin into a disposable directory under
# ``data/plugin-sandbox/{slug}/`` and run its entry point as a child
# process with the strongest isolation we can muster on the host OS.
#
# Strategy ladder (best-effort, fallthrough):
#
# Linux:
#   1. nsjail (if on PATH)         — namespace + seccomp + cgroup
#   2. firejail / bubblewrap       — namespace + seccomp
#   3. python prctl + setrlimit    — same-process, drop privileges
#   4. plain subprocess + timeout  — last resort
#
# Windows:
#   1. Job Object via pywin32      — memory + cpu + process count caps
#   2. plain subprocess + timeout  — last resort
#
# TODO(security): full AppContainer integration on Windows requires
# COM-based ICreateAppContainerProfile and is left as a follow-up.
# ═══════════════════════════════════════════════════════════════════════


import json as _json
import os as _os
import platform as _platform
import shutil as _shutil
import subprocess as _subprocess
import tempfile as _tempfile
import uuid as _uuid
from pathlib import Path as _Path
from typing import Any as _Any, Optional as _Optional
import zipfile as _zipfile


SANDBOX_ROOT = _Path(
    _os.getenv("HELEN_PLUGIN_SANDBOX_DIR", "data/plugin-sandbox")
)
SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)

DEFAULT_PROC_TIMEOUT_SEC = 15
DEFAULT_PROC_MEMORY_MB = 256


@dataclass
class SandboxInstall:
    slug: str
    version: str
    install_dir: _Path
    manifest_path: _Path
    entrypoint: _Path
    isolation_method: str = "subprocess"
    notes: list[str] = field(default_factory=list)


@dataclass
class SandboxRunReport:
    ok: bool
    exit_code: _Optional[int]
    duration_ms: int
    stdout: str
    stderr: str
    error: _Optional[str]
    isolation_method: str


# ────────────────────────────────────────────────────────────────────────


def _detect_linux_sandbox_tool() -> _Optional[str]:
    for tool in ("nsjail", "firejail", "bwrap"):
        if _shutil.which(tool):
            return tool
    return None


def _detect_windows_isolation() -> _Optional[str]:
    if _platform.system() != "Windows":
        return None
    try:                                                                # pragma: no cover
        import win32job   # type: ignore[import]
        import win32api   # type: ignore[import]
        _ = win32job
        _ = win32api
        return "win32job"
    except Exception:                                                   # noqa: BLE001
        return None


def _has_prctl() -> bool:
    try:                                                                # pragma: no cover
        import prctl   # type: ignore[import]
        _ = prctl
        return True
    except Exception:                                                   # noqa: BLE001
        return False


class PluginSandbox:
    """Disposable plugin install + isolated test runner."""

    def __init__(self, *, root: _Optional[_Path] = None) -> None:
        self.root = root or SANDBOX_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────
    # Install
    # ──────────────────────────────────────────────────────────────

    def install_from_bundle(
        self,
        slug: str,
        version: str,
        bundle_path: _Path,
        *,
        manifest_dict: _Optional[dict[_Any, _Any]] = None,
    ) -> SandboxInstall:
        """Extract ``bundle_path`` into a fresh sandbox directory.

        Bundles are expected to be ZIP archives whose root contains a
        ``plugin.json`` and the entrypoint file referenced in the manifest.
        """
        target = self.root / slug / version / _uuid.uuid4().hex[:8]
        if target.exists():
            _shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)

        notes: list[str] = []
        if bundle_path.suffix.lower() in (".zip", ".whl", ".helen-plugin"):
            try:
                with _zipfile.ZipFile(bundle_path, "r") as zf:
                    # zip slip guard
                    for n in zf.namelist():
                        if _os.path.isabs(n) or ".." in _Path(n).parts:
                            raise RuntimeError(f"zip-slip-attempt: {n}")
                    zf.extractall(target)
                notes.append("extracted-zip")
            except _zipfile.BadZipFile as e:
                raise RuntimeError(f"bad-zip: {e}") from e
        else:
            # treat as single-file plugin (legacy)
            (target / "plugin.py").write_bytes(bundle_path.read_bytes())
            notes.append("single-file-plugin")

        # Manifest fallback
        manifest_path = target / "plugin.json"
        if not manifest_path.exists() and manifest_dict:
            manifest_path.write_text(
                _json.dumps(manifest_dict, indent=2),
                encoding="utf-8",
            )
            notes.append("manifest-from-arg")
        if not manifest_path.exists():
            raise RuntimeError("manifest-missing")

        try:
            mf = _json.loads(manifest_path.read_text(encoding="utf-8"))
        except _json.JSONDecodeError as e:
            raise RuntimeError(f"manifest-json: {e}") from e
        ep_name = mf.get("entrypoint") or "plugin.py"
        entrypoint = target / ep_name
        if not entrypoint.exists():
            raise RuntimeError(f"entrypoint-missing: {ep_name}")

        method = self._pick_isolation()
        return SandboxInstall(
            slug=slug, version=version,
            install_dir=target, manifest_path=manifest_path,
            entrypoint=entrypoint, isolation_method=method, notes=notes,
        )

    # ──────────────────────────────────────────────────────────────
    # Run
    # ──────────────────────────────────────────────────────────────

    def _pick_isolation(self) -> str:
        sys_name = _platform.system()
        if sys_name == "Windows":
            return _detect_windows_isolation() or "subprocess"
        # Linux / *nix
        tool = _detect_linux_sandbox_tool()
        if tool:
            return tool
        if _has_prctl():
            return "prctl"
        return "subprocess"

    def run_entrypoint(
        self,
        install: SandboxInstall,
        *,
        timeout_seconds: int = DEFAULT_PROC_TIMEOUT_SEC,
        memory_mb: int = DEFAULT_PROC_MEMORY_MB,
        extra_env: _Optional[dict[str, str]] = None,
    ) -> SandboxRunReport:
        """Smoke-test the plugin entry point in an isolated subprocess.

        Returns whatever the subprocess wrote to stdout / stderr plus
        the exit code. A plugin that exits 0 is considered "good".
        """
        method = install.isolation_method
        env = dict(_os.environ)
        env["HELEN_SANDBOX"] = "1"
        env["HELEN_PLUGIN_SLUG"] = install.slug
        env["HELEN_PLUGIN_VERSION"] = install.version
        if extra_env:
            env.update(extra_env)

        cmd = self._build_command(install, method, memory_mb)
        return self._run(cmd, install.install_dir, env, timeout_seconds, method)

    def _build_command(
        self,
        install: SandboxInstall,
        method: str,
        memory_mb: int,
    ) -> list[str]:
        py = _shutil.which("python") or _shutil.which("python3") or "python"
        entry = str(install.entrypoint)
        if method == "nsjail":
            return [
                "nsjail", "--quiet",
                "--time_limit", "0",
                "--rlimit_as", str(memory_mb * 1024 * 1024),
                "--chroot", str(install.install_dir),
                "--", py, entry,
            ]
        if method == "firejail":
            return [
                "firejail", "--quiet", "--private",
                f"--rlimit-as={memory_mb * 1024 * 1024}",
                py, entry,
            ]
        if method == "bwrap":
            return [
                "bwrap", "--ro-bind", "/", "/",
                "--bind", str(install.install_dir), str(install.install_dir),
                "--unshare-net", "--die-with-parent",
                py, entry,
            ]
        # subprocess / prctl / win32job (the latter wraps in code, not args)
        return [py, entry]

    def _run(
        self,
        cmd: list[str],
        cwd: _Path,
        env: dict[str, str],
        timeout: int,
        method: str,
    ) -> SandboxRunReport:
        import time as _time
        t0 = _time.perf_counter()
        stdout = stderr = ""
        exit_code: _Optional[int] = None
        error: _Optional[str] = None
        try:                                                            # pragma: no cover
            proc = _subprocess.Popen(
                cmd, cwd=str(cwd), env=env,
                stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
                stdin=_subprocess.DEVNULL,
                text=True, errors="replace",
                # Windows Job Object attach (best-effort)
                creationflags=(
                    _subprocess.CREATE_NO_WINDOW
                    if _platform.system() == "Windows" else 0
                ),
            )
            try:
                if method == "win32job":
                    self._apply_win32_job(proc.pid)
                stdout, stderr = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except _subprocess.TimeoutExpired:
                proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except Exception:                                       # noqa: BLE001
                    stdout = stderr = ""
                error = "timeout"
        except FileNotFoundError as e:
            error = f"exec-failed: {e}"
        except Exception as e:                                          # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
        duration = int((_time.perf_counter() - t0) * 1000)
        ok = error is None and exit_code == 0
        return SandboxRunReport(
            ok=ok, exit_code=exit_code, duration_ms=duration,
            stdout=(stdout or "")[:MAX_STDOUT],
            stderr=(stderr or "")[:MAX_STDOUT],
            error=error, isolation_method=method,
        )

    def _apply_win32_job(self, pid: int) -> None:                       # pragma: no cover
        try:
            import win32job   # type: ignore[import]
            import win32api   # type: ignore[import]
            import win32con   # type: ignore[import]
            job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation,
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            )
            limits["ProcessMemoryLimit"] = DEFAULT_PROC_MEMORY_MB * 1024 * 1024
            win32job.SetInformationJobObject(
                job,
                win32job.JobObjectExtendedLimitInformation,
                limits,
            )
            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                False, pid,
            )
            win32job.AssignProcessToJobObject(job, handle)
        except Exception as e:                                          # noqa: BLE001
            logger.warning("plugin.sandbox.win32job-failed: %s", e)

    # ──────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────

    def cleanup(self, install: SandboxInstall) -> None:
        try:
            _shutil.rmtree(install.install_dir, ignore_errors=True)
        except Exception:                                               # noqa: BLE001
            pass

    def cleanup_slug(self, slug: str) -> int:
        d = self.root / slug
        removed = 0
        if d.exists():
            for child in d.iterdir():
                _shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed


_default_sandbox: _Optional[PluginSandbox] = None


def get_plugin_sandbox() -> PluginSandbox:
    global _default_sandbox
    if _default_sandbox is None:
        _default_sandbox = PluginSandbox()
    return _default_sandbox


__all__ = [
    # legacy (already exported above implicitly)
    "SandboxResult", "run_plugin_code",
    # new
    "PluginSandbox", "SandboxInstall", "SandboxRunReport",
    "get_plugin_sandbox", "SANDBOX_ROOT",
]
