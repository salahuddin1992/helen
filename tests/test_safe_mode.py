"""AllowlistGuard / safe-mode enforcement."""
import pytest
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_default_blocks_outside_allowlist(tmp_path):
    cfg = acp.RunnerConfig(fs_allowlist=[str(tmp_path / "ok")])
    (tmp_path / "ok").mkdir()
    g = acp.AllowlistGuard(cfg)
    assert g.is_allowed(tmp_path / "ok" / "file.txt") is True
    assert g.is_allowed(tmp_path / "other" / "file.txt") is False


def test_assert_raises(tmp_path):
    cfg = acp.RunnerConfig(fs_allowlist=[str(tmp_path / "ok")])
    (tmp_path / "ok").mkdir()
    g = acp.AllowlistGuard(cfg)
    with pytest.raises(PermissionError):
        g.assert_allowed(tmp_path / "outside.txt", op="write")


def test_disabling_safe_mode_passes(tmp_path):
    cfg = acp.RunnerConfig(safe_mode=False, fs_allowlist=[])
    g = acp.AllowlistGuard(cfg)
    g.assert_allowed(tmp_path / "anywhere.txt", op="write")


def test_auto_dir_always_allowed(tmp_path):
    """AUTO_DIR is always permitted regardless of allowlist."""
    cfg = acp.RunnerConfig(fs_allowlist=[])
    g = acp.AllowlistGuard(cfg)
    # AUTO_DIR is always added by AllowlistGuard._refresh
    assert g.is_allowed(acp.AUTO_DIR / "anything.txt") is True


def test_normalize_prompt_redirects(tmp_path):
    cfg = acp.RunnerConfig(allow_modify_existing_files=False)
    p = "update CLAUDE.md and DELIVERY-MANIFEST.md please"
    out = acp.normalize_prompt(p, cfg)
    assert "CLAUDE.md" not in out
    assert "DELIVERY-MANIFEST.md" not in out


def test_normalize_prompt_passthrough_when_allowed():
    cfg = acp.RunnerConfig(allow_modify_existing_files=True)
    p = "update CLAUDE.md please"
    out = acp.normalize_prompt(p, cfg)
    assert "CLAUDE.md" in out


def test_normalize_handles_empty():
    cfg = acp.RunnerConfig()
    assert acp.normalize_prompt("", cfg) == ""
    assert acp.normalize_prompt(None, cfg) is None


def test_safedeleter_unauthorized_refuses(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("x")
    sd = acp.SafeDeleter(root=tmp_path, authorized=False)
    assert sd.delete(target, reason="test") is None
    assert target.exists()  # NOT deleted
