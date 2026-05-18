"""dashboard token + auth checks."""
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_token_is_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "DASHBOARD_TOKEN_F", tmp_path / "tok.txt")
    t1 = acp._ensure_dashboard_token()
    t2 = acp._ensure_dashboard_token()
    assert t1 == t2 and len(t1) >= 32


def test_token_is_random(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "DASHBOARD_TOKEN_F", tmp_path / "tok1.txt")
    t1 = acp._ensure_dashboard_token()
    monkeypatch.setattr(acp, "DASHBOARD_TOKEN_F", tmp_path / "tok2.txt")
    t2 = acp._ensure_dashboard_token()
    assert t1 != t2


def test_handler_class_has_security_hooks():
    h = acp._DashboardHandlerSecure
    assert hasattr(h, "REQUIRE_AUTH")
    assert hasattr(h, "AUTH_TOKEN")
    assert hasattr(h, "LOOPBACK_ONLY")


def test_default_config_requires_auth():
    cfg = acp.RunnerConfig()
    assert cfg.dashboard_require_auth is True
    assert cfg.dashboard_bind_loopback_only is True
