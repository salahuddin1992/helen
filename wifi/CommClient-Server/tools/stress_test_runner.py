"""
Helen Stress Test Runner + Result Aggregator
============================================

Single command to run every Helen stress test (100, 1k, 10k, 100k mesh, 1M),
capture timings + resource consumption, and emit a consolidated JSON + Markdown
report + a CI-friendly JUnit XML artifact.

Usage:
    python tools/stress_test_runner.py --suite all
    python tools/stress_test_runner.py --suite mesh --tier 100 1000 10000
    python tools/stress_test_runner.py --suite federation --timeout 1800
    python tools/stress_test_runner.py --suite all --out artifacts/stress
    python tools/stress_test_runner.py --suite all --ci          # exit 1 on regression

Discovery:
    Tests live in:
        - C:\\Users\\youse\\c\\wifi\\Helen-Router\\stress_test_*.py
        - C:\\Users\\youse\\c\\wifi\\CommClient-Server\\tests\\stress\\test_stress_*.py (if present)
    The runner discovers both, runs them sequentially with resource sampling,
    and aggregates results.

Output:
    artifacts/stress/run-<timestamp>/
        summary.json
        summary.md
        junit.xml
        per-test/
            <test_name>.stdout.log
            <test_name>.stderr.log
            <test_name>.metrics.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from xml.etree import ElementTree as ET

logging.basicConfig(
    level=os.getenv("HELEN_STRESS_LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | stress | %(message)s",
)
log = logging.getLogger("helen.stress")

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Default discovery paths
# ---------------------------------------------------------------------------

WIFI_ROOT = Path(os.environ.get("HELEN_WIFI_ROOT", r"C:\Users\youse\c\wifi"))
DEFAULT_DISCOVER_DIRS = [
    WIFI_ROOT / "Helen-Router",
    WIFI_ROOT / "CommClient-Server" / "tests" / "stress",
    WIFI_ROOT / "Helen-Rendezvous" / "tests" / "stress",
]


SUITE_MAP = {
    "mesh": [
        "stress_test_100.py",
        "stress_test_1000.py",
        "stress_test_10000.py",
        "stress_test_100k_mesh.py",
        "stress_test_1m.py",
    ],
    "federation": [
        "test_cross_platform_federation.py",
    ],
    "external": [
        "test_external_capacity.py",
    ],
    "failover": [
        "test_failover.py",
    ],
}


def _all_suite_files() -> List[str]:
    seen: List[str] = []
    for files in SUITE_MAP.values():
        for f in files:
            if f not in seen:
                seen.append(f)
    return seen


SUITE_MAP["all"] = _all_suite_files()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class TestRun:
    name: str
    path: Path
    tier: Optional[int] = None  # 100 / 1000 / etc
    suite: str = "unknown"

    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_seconds: float = 0.0
    exit_code: int = -1
    timed_out: bool = False

    rss_max_mb: float = 0.0
    cpu_user_sec: float = 0.0
    cpu_system_sec: float = 0.0

    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    extracted_metrics: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class SuiteReport:
    started_at: str
    finished_at: str
    host: Dict[str, Any]
    runs: List[TestRun]
    counts: Dict[str, int] = field(default_factory=dict)
    regressions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "host": self.host,
            "counts": self.counts,
            "regressions": self.regressions,
            "runs": [_run_to_dict(r) for r in self.runs],
        }


def _run_to_dict(r: TestRun) -> Dict[str, Any]:
    d = dataclasses.asdict(r)
    d["path"] = str(r.path)
    d["passed"] = r.passed
    return d


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_tests(suite: str, tier_filter: Optional[List[int]]) -> List[Path]:
    if suite not in SUITE_MAP:
        raise ValueError(f"Unknown suite '{suite}'. Available: {sorted(SUITE_MAP)}")
    target_files = SUITE_MAP[suite]

    found: List[Path] = []
    for d in DEFAULT_DISCOVER_DIRS:
        if not d.exists():
            continue
        for fname in target_files:
            p = d / fname
            if p.exists():
                if tier_filter is not None:
                    tier = _extract_tier(fname)
                    if tier is not None and tier not in tier_filter:
                        continue
                found.append(p)
    return sorted(set(found))


def _extract_tier(filename: str) -> Optional[int]:
    """Parse the number suffix from filenames like stress_test_100k_mesh.py."""
    base = filename.replace(".py", "")
    # Look for numeric tokens
    digits = ""
    multiplier = 1
    for chunk in base.split("_"):
        chunk = chunk.lower()
        if chunk.endswith("k") and chunk[:-1].isdigit():
            return int(chunk[:-1]) * 1000
        if chunk.endswith("m") and chunk[:-1].isdigit():
            return int(chunk[:-1]) * 1_000_000
        if chunk.isdigit():
            digits = chunk
    return int(digits) * multiplier if digits else None


# ---------------------------------------------------------------------------
# Resource sampling
# ---------------------------------------------------------------------------


def _sample_process(proc: subprocess.Popen) -> Dict[str, float]:
    """Best-effort RSS/CPU sample via psutil; degrade gracefully."""
    try:
        import psutil  # type: ignore

        p = psutil.Process(proc.pid)
        with p.oneshot():
            mem = p.memory_info().rss / (1024 * 1024)
            cpu = p.cpu_times()
            return {"rss_mb": mem, "cpu_user": cpu.user, "cpu_system": cpu.system}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_test(
    path: Path,
    suite: str,
    out_dir: Path,
    timeout_sec: int,
    extra_args: Optional[List[str]] = None,
    python_exec: Optional[str] = None,
) -> TestRun:
    run = TestRun(
        name=path.stem,
        path=path,
        tier=_extract_tier(path.name),
        suite=suite,
    )
    py = python_exec or sys.executable
    cmd: List[str] = [py, str(path)] + (extra_args or [])
    log.info("→ running %s tier=%s", path.name, run.tier)
    log.info("  cmd: %s", " ".join(shlex.quote(c) for c in cmd))

    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / f"{run.name}.stdout.log"
    stderr_path = out_dir / f"{run.name}.stderr.log"
    metrics_path = out_dir / f"{run.name}.metrics.json"

    run.stdout_path = str(stdout_path)
    run.stderr_path = str(stderr_path)

    start_ts = time.time()
    run.started_at = datetime.now(UTC).isoformat()

    with stdout_path.open("w", encoding="utf-8") as so, stderr_path.open("w", encoding="utf-8") as se:
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=so,
                stderr=se,
                cwd=str(path.parent),
                env=os.environ.copy(),
            )
        except FileNotFoundError as exc:
            run.error_message = f"spawn failed: {exc}"
            run.exit_code = 127
            run.ended_at = datetime.now(UTC).isoformat()
            run.duration_seconds = time.time() - start_ts
            return run

        # Resource sampling loop
        peak_rss = 0.0
        cpu_user = 0.0
        cpu_sys = 0.0
        try:
            while True:
                ret = proc.poll()
                if ret is not None:
                    break
                # Timeout check
                if time.time() - start_ts > timeout_sec:
                    log.warning("timeout exceeded for %s, killing", run.name)
                    run.timed_out = True
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                sample = _sample_process(proc)
                if sample:
                    peak_rss = max(peak_rss, sample.get("rss_mb", 0.0))
                    cpu_user = max(cpu_user, sample.get("cpu_user", 0.0))
                    cpu_sys = max(cpu_sys, sample.get("cpu_system", 0.0))
                time.sleep(0.5)
        finally:
            run.exit_code = proc.returncode if proc.returncode is not None else -1

    run.ended_at = datetime.now(UTC).isoformat()
    run.duration_seconds = round(time.time() - start_ts, 3)
    run.rss_max_mb = round(peak_rss, 2)
    run.cpu_user_sec = round(cpu_user, 2)
    run.cpu_system_sec = round(cpu_sys, 2)

    # Extract metrics from stdout (Helen tests print JSON lines starting with "METRIC ")
    run.extracted_metrics = _scan_metrics(stdout_path)
    metrics_path.write_text(json.dumps(run.extracted_metrics, indent=2), encoding="utf-8")

    status = "PASS" if run.passed else ("TIMEOUT" if run.timed_out else "FAIL")
    log.info(
        "  %s in %.1fs (rss_max=%.1f MB, cpu_user=%.1fs)",
        status,
        run.duration_seconds,
        run.rss_max_mb,
        run.cpu_user_sec,
    )
    return run


def _scan_metrics(stdout_path: Path) -> Dict[str, Any]:
    """Extract structured metrics from stdout — supports two conventions:

    1) Lines starting with 'METRIC ' followed by JSON: METRIC {"throughput": 1000}
    2) Final SUMMARY block delimited by '----- SUMMARY -----' / '----- END -----'.
    """
    metrics: Dict[str, Any] = {}
    if not stdout_path.exists():
        return metrics
    try:
        with stdout_path.open("r", encoding="utf-8", errors="replace") as fh:
            in_summary = False
            summary_lines: List[str] = []
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("METRIC "):
                    try:
                        obj = json.loads(line[len("METRIC ") :])
                        if isinstance(obj, dict):
                            metrics.update(obj)
                    except Exception:
                        pass
                elif "----- SUMMARY -----" in line:
                    in_summary = True
                elif "----- END -----" in line:
                    in_summary = False
                elif in_summary:
                    summary_lines.append(line)
            if summary_lines:
                # Try parsing as JSON or as "key: value" pairs
                blob = "\n".join(summary_lines).strip()
                try:
                    obj = json.loads(blob)
                    if isinstance(obj, dict):
                        metrics.update(obj)
                except Exception:
                    for ln in summary_lines:
                        if ":" in ln:
                            k, v = ln.split(":", 1)
                            k = k.strip().lower().replace(" ", "_")
                            v = v.strip()
                            try:
                                metrics[k] = float(v) if "." in v else int(v)
                            except ValueError:
                                metrics[k] = v
    except Exception as exc:
        log.debug("scan_metrics failed: %s", exc)
    return metrics


# ---------------------------------------------------------------------------
# Aggregation / reporting
# ---------------------------------------------------------------------------


def host_snapshot() -> Dict[str, Any]:
    info = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "hostname": platform.node(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        import psutil  # type: ignore

        info["memory_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 2)
        info["disk_total_gb"] = round(psutil.disk_usage("/").total / (1024 ** 3), 2)
    except Exception:
        pass
    return info


def write_summary_json(report: SuiteReport, out_dir: Path) -> Path:
    p = out_dir / "summary.json"
    p.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("summary.json → %s", p)
    return p


def write_summary_md(report: SuiteReport, out_dir: Path) -> Path:
    lines: List[str] = []
    lines.append("# Helen Stress Test Run")
    lines.append("")
    lines.append(f"- Started: `{report.started_at}`")
    lines.append(f"- Finished: `{report.finished_at}`")
    lines.append(f"- Host: `{report.host.get('hostname')}` ({report.host.get('platform')})")
    lines.append(f"- CPU cores: {report.host.get('cpu_count')}, RAM: {report.host.get('memory_total_gb', '?')} GB")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    for k in ("total", "passed", "failed", "timed_out"):
        lines.append(f"- **{k}**: {report.counts.get(k, 0)}")
    if report.regressions:
        lines.append("")
        lines.append("## ⚠ Regressions")
        for r in report.regressions:
            lines.append(f"- {r}")
    lines.append("")
    lines.append("## Per-test results")
    lines.append("")
    lines.append("| Test | Tier | Suite | Status | Duration (s) | RSS max (MB) | CPU user (s) |")
    lines.append("|------|-----:|-------|--------|-------------:|-------------:|-------------:|")
    for r in report.runs:
        status = "✅ PASS" if r.passed else ("⏱ TIMEOUT" if r.timed_out else "❌ FAIL")
        lines.append(
            f"| `{r.name}` | {r.tier or ''} | {r.suite} | {status} | "
            f"{r.duration_seconds} | {r.rss_max_mb} | {r.cpu_user_sec} |"
        )
    lines.append("")
    lines.append("## Extracted metrics")
    lines.append("")
    for r in report.runs:
        if r.extracted_metrics:
            lines.append(f"### {r.name}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(r.extracted_metrics, indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")
    p = out_dir / "summary.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    log.info("summary.md → %s", p)
    return p


def write_junit_xml(report: SuiteReport, out_dir: Path) -> Path:
    suites = ET.Element("testsuites")
    suites.set("name", "helen.stress")
    suites.set("tests", str(report.counts.get("total", len(report.runs))))
    suites.set("failures", str(report.counts.get("failed", 0)))
    suites.set("errors", str(report.counts.get("timed_out", 0)))

    by_suite: Dict[str, List[TestRun]] = {}
    for r in report.runs:
        by_suite.setdefault(r.suite, []).append(r)

    for suite_name, runs in by_suite.items():
        suite_el = ET.SubElement(suites, "testsuite")
        suite_el.set("name", f"helen.stress.{suite_name}")
        suite_el.set("tests", str(len(runs)))
        suite_el.set("failures", str(sum(1 for r in runs if not r.passed and not r.timed_out)))
        suite_el.set("errors", str(sum(1 for r in runs if r.timed_out)))
        suite_el.set("time", f"{sum(r.duration_seconds for r in runs):.3f}")
        for r in runs:
            case = ET.SubElement(suite_el, "testcase")
            case.set("classname", f"helen.stress.{suite_name}")
            case.set("name", r.name)
            case.set("time", f"{r.duration_seconds:.3f}")
            if r.timed_out:
                err = ET.SubElement(case, "error", type="Timeout")
                err.text = f"Exceeded timeout. exit_code={r.exit_code}"
            elif not r.passed:
                fail = ET.SubElement(case, "failure", type="NonZeroExit")
                fail.text = (
                    f"exit_code={r.exit_code}; error={r.error_message or 'see stderr log'}; "
                    f"stderr_path={r.stderr_path}"
                )

    tree = ET.ElementTree(suites)
    p = out_dir / "junit.xml"
    tree.write(p, encoding="utf-8", xml_declaration=True)
    log.info("junit.xml → %s", p)
    return p


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


def detect_regressions(report: SuiteReport, baseline_path: Optional[Path]) -> List[str]:
    if not baseline_path or not baseline_path.exists():
        return []
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load baseline: %s", exc)
        return []

    baseline_runs = {r["name"]: r for r in baseline.get("runs", [])}
    regressions: List[str] = []

    for r in report.runs:
        base = baseline_runs.get(r.name)
        if not base:
            continue
        # 30% slower or 50% higher RSS counts as regression
        base_duration = base.get("duration_seconds", 0)
        if base_duration > 0 and r.duration_seconds > base_duration * 1.30:
            regressions.append(
                f"{r.name}: duration {r.duration_seconds:.1f}s > 130% of baseline {base_duration:.1f}s"
            )
        base_rss = base.get("rss_max_mb", 0)
        if base_rss > 0 and r.rss_max_mb > base_rss * 1.50:
            regressions.append(
                f"{r.name}: rss_max_mb {r.rss_max_mb:.1f} > 150% of baseline {base_rss:.1f}"
            )
        if base.get("passed") and not r.passed:
            regressions.append(f"{r.name}: previously PASS, now FAIL/TIMEOUT")

    return regressions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Helen stress-test runner + aggregator")
    p.add_argument(
        "--suite",
        default="all",
        choices=list(SUITE_MAP.keys()),
        help="Which suite to run (default: all)",
    )
    p.add_argument(
        "--tier",
        type=int,
        nargs="*",
        help="Filter mesh tier(s): 100, 1000, 10000, 100000, 1000000",
    )
    p.add_argument("--out", default="artifacts/stress", help="Output directory")
    p.add_argument("--timeout", type=int, default=3600, help="Per-test timeout in seconds")
    p.add_argument("--python", default=None, help="Python executable to use")
    p.add_argument("--baseline", default=None, help="Path to baseline summary.json for regression detection")
    p.add_argument("--ci", action="store_true", help="Exit 1 on any failure or regression")
    p.add_argument("--list", action="store_true", help="List discovered tests and exit")
    p.add_argument("--dry-run", action="store_true", help="Plan-only, do not execute")
    p.add_argument("--extra-arg", action="append", default=[], help="Extra args passed to each test")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tier_filter = args.tier if args.tier else None
    paths = discover_tests(args.suite, tier_filter)

    if args.list or args.dry_run:
        for p in paths:
            print(f"{p}  (tier={_extract_tier(p.name)})")
        if args.dry_run and not paths:
            log.warning("no tests discovered for suite=%s tier=%s", args.suite, tier_filter)
        return 0

    if not paths:
        log.error("no stress tests found for suite='%s' tier=%s", args.suite, tier_filter)
        return 2

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_root = Path(args.out) / f"run-{ts}"
    out_root.mkdir(parents=True, exist_ok=True)
    per_test_dir = out_root / "per-test"

    host = host_snapshot()
    started_at = datetime.now(UTC).isoformat()
    runs: List[TestRun] = []

    for path in paths:
        # determine which suite category this test belongs to
        belongs = "unknown"
        for cat, fnames in SUITE_MAP.items():
            if cat == "all":
                continue
            if path.name in fnames:
                belongs = cat
                break
        run = run_test(
            path=path,
            suite=belongs,
            out_dir=per_test_dir,
            timeout_sec=args.timeout,
            extra_args=args.extra_arg,
            python_exec=args.python,
        )
        runs.append(run)

    finished_at = datetime.now(UTC).isoformat()
    total = len(runs)
    passed = sum(1 for r in runs if r.passed)
    failed = sum(1 for r in runs if not r.passed and not r.timed_out)
    timed_out = sum(1 for r in runs if r.timed_out)

    report = SuiteReport(
        started_at=started_at,
        finished_at=finished_at,
        host=host,
        runs=runs,
        counts={"total": total, "passed": passed, "failed": failed, "timed_out": timed_out},
    )
    report.regressions = detect_regressions(report, Path(args.baseline) if args.baseline else None)

    write_summary_json(report, out_root)
    write_summary_md(report, out_root)
    write_junit_xml(report, out_root)

    log.info("=" * 60)
    log.info("RESULT: total=%d passed=%d failed=%d timed_out=%d", total, passed, failed, timed_out)
    if report.regressions:
        log.warning("REGRESSIONS detected: %d", len(report.regressions))
        for r in report.regressions:
            log.warning("  • %s", r)

    # Copy latest run pointer
    latest = Path(args.out) / "latest"
    try:
        if latest.exists() or latest.is_symlink():
            if latest.is_symlink() or latest.is_file():
                latest.unlink()
            else:
                shutil.rmtree(latest)
        try:
            latest.symlink_to(out_root.name, target_is_directory=True)
        except (OSError, NotImplementedError):
            shutil.copytree(out_root, latest, dirs_exist_ok=True)
    except Exception as exc:
        log.debug("could not create 'latest' pointer: %s", exc)

    if args.ci:
        if failed or timed_out or report.regressions:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
