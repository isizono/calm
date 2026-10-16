"""hooks/recorder_guard_hook.py の単体テスト。

stdinにPreToolUseのhook入力を流してmain()を呼び、stdoutの判定を読む。
run_dirのrun.json・cursor.jsonは見張りが実際に書く形で用意する。
"""
import io
import json
import sys

import pytest

from hooks import recorder_guard_hook as guard


@pytest.fixture(autouse=True)
def _isolate_signal_db(temp_db):
    """deny経路がguard_block signalをDBへ書くため、一時DBへ固定する。"""
    return temp_db


def _run_dir(tmp_path, pending) -> object:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"main_sid": "main-1"}), encoding="utf-8")
    (run_dir / "cursor.json").write_text(json.dumps({"pending": pending}), encoding="utf-8")
    return run_dir


def _decide(run_dir, session_id: str, capsys, tool_name: str = "mcp__calm__add_logs") -> str | None:
    event = {"session_id": session_id, "tool_name": tool_name, "tool_input": {}, "hook_event_name": "PreToolUse"}
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        guard.main([str(run_dir)])
    finally:
        sys.stdin = sys.__stdin__
    text = capsys.readouterr().out.strip()
    if not text or text == "{}":
        return None
    return json.loads(text)["hookSpecificOutput"]["permissionDecision"]


def test_denies_before_any_shard(tmp_path, capsys):
    assert _decide(_run_dir(tmp_path, None), "rec-1", capsys) == "deny"


@pytest.mark.parametrize("tool_name", ["Read", "ToolSearch", "mcp__plugin_calm_calm__search"])
def test_denies_every_tool_before_shard(tmp_path, capsys, tool_name):
    assert _decide(_run_dir(tmp_path, None), "rec-1", capsys, tool_name) == "deny"


def test_allows_the_recipient_of_the_pending_shard(tmp_path, capsys):
    run_dir = _run_dir(tmp_path, {"no": 3, "recipient_sid": "rec-1"})
    assert _decide(run_dir, "rec-1", capsys) is None


def test_denies_a_restarted_recorder_until_the_shard_is_resent(tmp_path, capsys):
    """片の処理中に落ちて立て直された記録役は、pendingが残っていても
    送り先が前の記録役のままなので、再送されるまで拒否される。"""
    run_dir = _run_dir(tmp_path, {"no": 3, "recipient_sid": "rec-old"})
    assert _decide(run_dir, "rec-new", capsys) == "deny"


def test_denies_pending_without_recipient(tmp_path, capsys):
    """送り先を書く前の版の見張りが残したpendingは、再送されるまで拒否する。"""
    assert _decide(_run_dir(tmp_path, {"no": 3}), "rec-1", capsys) == "deny"


def test_allows_outside_a_recorder_run_dir(tmp_path, capsys):
    assert _decide(tmp_path, "anyone", capsys) is None


def test_allows_when_cursor_is_unreadable(tmp_path, capsys):
    run_dir = _run_dir(tmp_path, None)
    (run_dir / "cursor.json").write_text("{broken", encoding="utf-8")
    assert _decide(run_dir, "rec-1", capsys) is None


def test_deny_records_guard_block_signal(tmp_path, capsys, temp_db):
    from src.db import get_connection

    _decide(_run_dir(tmp_path, None), "rec-1", capsys)
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events WHERE kind = 'guard_block'").fetchone()
    finally:
        conn.close()
    assert row["source"] == "hook:recorder_guard"
