"""CLI argument plumbing — argparse should accept all v4 verbs."""
import sys
from pathlib import Path
SCRIPT_DIR = Path(r"C:\Users\youse\AI")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import autonomous_claude_pro as acp


def test_v4_parser_has_safety_flags():
    p = acp._build_argparser_v4()
    actions = {a.dest for a in p._actions}
    for needed in ("dry_run", "preflight", "regen_plugin_signatures",
                   "print_dashboard_token", "allow_dangerous",
                   "unsafe_mode", "allow_modify_existing", "write_tests"):
        assert needed in actions, f"missing CLI arg: {needed}"


def test_v3_parser_has_ultra():
    p = acp._build_argparser_v3()
    actions = {a.dest for a in p._actions}
    assert "ultra" in actions
    assert "stop" in actions
    assert "allow_conflict_deletion" in actions


def test_v2_parser_has_basic_flags():
    p = acp._build_argparser_v2()
    actions = {a.dest for a in p._actions}
    for needed in ("scan", "scan_to_queue", "autodoc", "backup",
                   "git_auto_commit", "no_dashboard"):
        assert needed in actions


def test_default_flags_no_longer_dangerous():
    """The default flags list must NOT carry dangerously-skip-permissions."""
    assert acp.DANGEROUS_FLAG not in acp.DEFAULT_CLAUDE_FLAGS
    assert acp.DEFAULT_CLAUDE_FLAGS == [] or all(
        f != acp.DANGEROUS_FLAG for f in acp.DEFAULT_CLAUDE_FLAGS
    )


def test_dry_run_argv_uses_echo():
    """build_argv() in dry-run mode must NOT shell out to claude."""
    cfg = acp.RunnerConfig(dry_run=True)

    # Use a dummy ClaudeProcess just to call build_argv.
    cp = acp.ClaudeProcess(
        cfg=cfg, prompt="test", use_continue=False, cycle=1,
        on_log=lambda *a, **k: None,
    )
    argv = cp.build_argv()
    cmd = " ".join(argv).lower()
    assert "claude" not in argv[0].lower() or "cmd" in argv[0].lower() or "/echo" in argv[0]
    # Must contain the dry_run marker JSON.
    full = " ".join(argv)
    assert "dry_run" in full


def test_dangerous_flag_from_config():
    """If cfg.allow_dangerous_permissions=True, --dangerously-skip-permissions
    should appear in argv. (only checked when claude resolver returns
    something — we just verify the logic switch by inspecting argv shape.)"""
    cfg = acp.RunnerConfig(allow_dangerous_permissions=True, dry_run=True)
    # In dry_run we short-circuit, so test the non-dry-run path by
    # patching _resolve_claude.
    cfg.dry_run = False
    cp = acp.ClaudeProcess(
        cfg=cfg, prompt="x", use_continue=False, cycle=1,
        on_log=lambda *a, **k: None,
    )
    # Patch resolver via class-level monkeypatch.
    original = acp.ClaudeProcess._resolve_claude
    try:
        acp.ClaudeProcess._resolve_claude = staticmethod(lambda: r"C:\fake\claude.cmd")
        argv = cp.build_argv()
        assert acp.DANGEROUS_FLAG in argv
    finally:
        acp.ClaudeProcess._resolve_claude = original


def test_dangerous_flag_off_when_config_false():
    cfg = acp.RunnerConfig(allow_dangerous_permissions=False)
    cp = acp.ClaudeProcess(
        cfg=cfg, prompt="x", use_continue=False, cycle=1,
        on_log=lambda *a, **k: None,
    )
    original = acp.ClaudeProcess._resolve_claude
    # Also temporarily clear ACP_ALLOW_DANGEROUS env if set.
    import os as _os
    saved = _os.environ.pop("ACP_ALLOW_DANGEROUS", None)
    try:
        acp.ClaudeProcess._resolve_claude = staticmethod(lambda: r"C:\fake\claude.cmd")
        argv = cp.build_argv()
        assert acp.DANGEROUS_FLAG not in argv
    finally:
        acp.ClaudeProcess._resolve_claude = original
        if saved is not None:
            _os.environ["ACP_ALLOW_DANGEROUS"] = saved
