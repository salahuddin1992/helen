"""secret redaction + plugin signature guard."""
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_redacts_anthropic_key():
    txt = "use sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA right now"
    out = acp.redact_secrets(txt)
    assert "sk-ant-api03-A" not in out
    assert "REDACTED" in out


def test_redacts_password_assignment():
    txt = "password='hunter2-very-secret-xyz'"
    out = acp.redact_secrets(txt)
    assert "hunter2" not in out


def test_redacts_pem_block():
    pem = "-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----"
    assert "ABCDEF" not in acp.redact_secrets(pem)


def test_redacts_aws_key():
    out = acp.redact_secrets("AKIAIOSFODNN7EXAMPLE access denied")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_idempotent():
    once = acp.redact_secrets("token=abc123def456")
    twice = acp.redact_secrets(once)
    assert once == twice


def test_handles_empty():
    assert acp.redact_secrets("") == ""
    assert acp.redact_secrets(None) == ""


def test_plugin_signature_rejects_unsigned(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(acp, "PLUGIN_SIG_FILE", tmp_path / "sigs.json")
    (tmp_path / "evil.py").write_text("print('hi')")
    cfg = acp.RunnerConfig()
    assert cfg.require_signed_plugins is True
    guard = acp.PluginSignatureGuard(cfg)
    assert guard.is_trusted(tmp_path / "evil.py") is False


def test_plugin_signature_accepts_signed(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(acp, "PLUGIN_SIG_FILE", tmp_path / "sigs.json")
    p = tmp_path / "ok.py"
    p.write_text("# trusted")
    cfg = acp.RunnerConfig()
    guard = acp.PluginSignatureGuard(cfg)
    sigs = guard.regenerate_signatures()
    assert "ok.py" in sigs
    assert guard.is_trusted(p) is True


def test_plugin_signature_disabled_allows_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(acp, "PLUGIN_SIG_FILE", tmp_path / "sigs.json")
    cfg = acp.RunnerConfig(require_signed_plugins=False)
    p = tmp_path / "x.py"; p.write_text("# anything")
    guard = acp.PluginSignatureGuard(cfg)
    assert guard.is_trusted(p) is True


def test_signature_detects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(acp, "PLUGIN_SIG_FILE", tmp_path / "sigs.json")
    p = tmp_path / "p.py"
    p.write_text("# original")
    cfg = acp.RunnerConfig()
    guard = acp.PluginSignatureGuard(cfg)
    guard.regenerate_signatures()
    assert guard.is_trusted(p) is True
    p.write_text("# TAMPERED")
    assert guard.is_trusted(p) is False
