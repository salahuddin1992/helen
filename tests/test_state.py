"""RunnerState round-trip + checkpoint behaviour."""
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "STATE_F", tmp_path / "s.json")
    s = acp.RunnerState()
    s.cycle = 7; s.success_total = 5; s.fail_total = 2
    s.save()
    s2 = acp.RunnerState.load()
    assert s2.cycle == 7
    assert s2.success_total == 5
    assert s2.fail_total == 2


def test_state_handles_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "STATE_F", tmp_path / "missing.json")
    s = acp.RunnerState.load()
    assert s.cycle == 0


def test_state_handles_corrupt(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(acp, "STATE_F", p)
    s = acp.RunnerState.load()
    assert s.cycle == 0


def test_state_to_json_keys():
    s = acp.RunnerState()
    data = s.to_json()
    assert '"cycle"' in data
    assert '"success_total"' in data
    assert '"fingerprint"' in data
