"""FailureAnalyzer pattern matching."""
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_module_not_found(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("ModuleNotFoundError: No module named 'requests'\n")
    a = acp.FailureAnalyzer()
    fix = a.analyze(err, None)
    assert fix and "requests" in fix


def test_permission_error(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("PermissionError: [WinError 5] Access is denied\n")
    a = acp.FailureAnalyzer()
    fix = a.analyze(err, None)
    assert fix and "Permission" in fix


def test_address_in_use(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("OSError: [WinError 10048] Address already in use\n")
    a = acp.FailureAnalyzer()
    fix = a.analyze(err, None)
    assert fix and "port" in fix.lower()


def test_timeout(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("requests.exceptions.Timeout: timed out after 30s\n")
    a = acp.FailureAnalyzer()
    fix = a.analyze(err, None)
    assert fix and "Timeout" in fix


def test_assertion_error(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("AssertionError: expected 1 got 2\n")
    a = acp.FailureAnalyzer()
    fix = a.analyze(err, None)
    assert fix and "Assertion" in fix


def test_no_match_returns_none(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("just a benign line\n")
    a = acp.FailureAnalyzer()
    assert a.analyze(err, None) is None


def test_handles_missing_files():
    a = acp.FailureAnalyzer()
    assert a.analyze(None, None) is None


def test_handles_missing_paths(tmp_path):
    a = acp.FailureAnalyzer()
    nonexistent = tmp_path / "nope.log"
    # Should return None gracefully even if files don't exist.
    assert a.analyze(nonexistent, None) is None
