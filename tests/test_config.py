"""tests for RunnerConfig safe defaults + persistence."""
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_safe_defaults():
    c = acp.RunnerConfig()
    assert c.allow_dangerous_permissions is False
    assert c.safe_mode is True
    assert c.dry_run is False
    assert c.git_auto_commit_enabled is False
    assert c.require_signed_plugins is True
    assert c.dashboard_require_auth is True
    assert c.dashboard_bind_loopback_only is True
    assert c.redact_secrets_in_logs is True
    assert c.allow_modify_existing_files is False


def test_default_flags_no_dangerous():
    """The DEFAULT_CLAUDE_FLAGS must NOT contain --dangerously-skip-permissions."""
    assert acp.DANGEROUS_FLAG not in acp.DEFAULT_CLAUDE_FLAGS


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "CONFIG_F", tmp_path / "c.json")
    c1 = acp.RunnerConfig()
    c1.model = "claude-haiku-test"
    c1.save()
    c2 = acp.RunnerConfig.load()
    assert c2.model == "claude-haiku-test"


def test_extra_flags_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "CONFIG_F", tmp_path / "c.json")
    c = acp.RunnerConfig()
    c.extra_claude_flags = ["--foo", "--bar=1"]
    c.save()
    c2 = acp.RunnerConfig.load()
    assert c2.extra_claude_flags == ["--foo", "--bar=1"]


def test_unknown_keys_ignored(tmp_path, monkeypatch):
    """Loading config with unknown keys should not crash."""
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text('{"model":"x","mystery":"value"}', encoding="utf-8")
    monkeypatch.setattr(acp, "CONFIG_F", cfg_path)
    c = acp.RunnerConfig.load()
    assert c.model == "x"
