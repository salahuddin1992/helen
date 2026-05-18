"""tests for the TaskQueue (priority + primary lanes)."""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_push_pop_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "QUEUE_F", tmp_path / "q.txt")
    monkeypatch.setattr(acp, "PRIORITY_F", tmp_path / "p.txt")
    q = acp.TaskQueue(primary=tmp_path / "q.txt", priority=tmp_path / "p.txt")
    q.push("alpha")
    q.push("bravo", priority=True)
    line, src = q.pop()
    assert line == "bravo" and src == "priority"
    line, src = q.pop()
    assert line == "alpha" and src == "queue"
    line, src = q.pop()
    assert line is None and src == "empty"


def test_skip_comments_and_blanks(tmp_path):
    q = acp.TaskQueue(primary=tmp_path / "q.txt", priority=tmp_path / "p.txt")
    q.push("# this is a comment")
    q.push("")
    q.push("real-task")
    line, src = q.pop()
    assert line == "real-task"


def test_depth_count(tmp_path):
    q = acp.TaskQueue(primary=tmp_path / "q.txt", priority=tmp_path / "p.txt")
    q.push("a"); q.push("b"); q.push("c", priority=True)
    prio, prim = q.depth()
    assert prio == 1 and prim == 2


def test_push_many(tmp_path):
    q = acp.TaskQueue(primary=tmp_path / "q.txt", priority=tmp_path / "p.txt")
    n = q.push_many(["a", "# skip", "b", ""])
    assert n == 2


def test_clear(tmp_path):
    q = acp.TaskQueue(primary=tmp_path / "q.txt", priority=tmp_path / "p.txt")
    q.push("a"); q.push("b", priority=True)
    removed = q.clear()
    assert removed == 2
    prio, prim = q.depth()
    assert prio == 0 and prim == 0
