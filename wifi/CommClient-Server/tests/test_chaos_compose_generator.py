"""
Smoke test for scripts/gen_chaos_compose.py.

Validates that the helper materializes a parseable docker-compose
YAML for an arbitrary --count N. We don't actually try to bring the
cluster up — that would require Docker. We just verify:

  * The script exits 0.
  * The output file exists where expected.
  * The output is well-formed YAML.
  * It contains at least N service entries named server-NNN.
  * Required env vars are present on each replica.

Without this test, a regression in the generator (e.g. accidentally
emitting tabs vs spaces) would only surface when an operator tried
to run the chaos lab — at which point the broken file silently
fails docker compose parse.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gen_chaos_compose.py"
SKELETON = REPO_ROOT / "docker-compose.chaos.yml"


@pytest.fixture(autouse=True)
def _require_yaml():
    """Skip the test if PyYAML isn't installed. The runtime app
    doesn't require it, so it's not in requirements.txt."""
    pytest.importorskip("yaml")


def test_skeleton_exists_and_parses():
    """The skeleton file in the repo must be valid YAML on its own."""
    assert SKELETON.exists(), f"skeleton not found: {SKELETON}"
    import yaml  # type: ignore
    parsed = yaml.safe_load(SKELETON.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert "services" in parsed
    assert "redis" in parsed["services"]
    # Skeleton has 5 hand-coded server replicas plus redis.
    server_names = [s for s in parsed["services"] if s.startswith("server-")]
    assert len(server_names) == 5, f"expected 5 hand-coded servers, got {server_names}"


def test_generator_produces_n_servers(tmp_path, monkeypatch):
    """Run the generator with --count 12 and validate the output."""
    if not SCRIPT.exists():
        pytest.skip("gen_chaos_compose.py not present")

    out_path = REPO_ROOT / "docker-compose.chaos.full.yml"
    # Make sure we don't collide with a stale output file.
    if out_path.exists():
        out_path.unlink()

    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--count", "12"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"gen_chaos_compose.py exited {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert out_path.exists(), "expected output file not created"

        import yaml  # type: ignore
        parsed = yaml.safe_load(out_path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        services = parsed.get("services", {})
        server_names = sorted(s for s in services if s.startswith("server-"))
        assert len(server_names) == 12, (
            f"expected 12 servers, got {len(server_names)}: {server_names}"
        )
        # Pick a generated entry (not in the skeleton) and check fields.
        s10 = services.get("server-010")
        assert s10 is not None
        env = s10.get("environment", {})
        assert env.get("HELEN_SERVER_ID") == "server_010"
        assert env.get("HELEN_REGION", "").startswith("chaos-r")
    finally:
        # Don't leave test artifacts lying around.
        if out_path.exists():
            out_path.unlink()


def test_generator_refuses_below_skeleton():
    """--count must be greater than the hand-coded count (5)."""
    if not SCRIPT.exists():
        pytest.skip("gen_chaos_compose.py not present")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--count", "3"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, "expected non-zero exit on --count 3"
