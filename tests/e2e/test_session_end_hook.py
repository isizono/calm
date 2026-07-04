"""hooks/session_end_hook.py の E2E テスト

subprocess.run で session_end_hook.py を呼び出し、stdin→stdout の入出力をテスト。
auto-sync起動パスではモック claude スクリプトを PATH に配置して検証。
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --- ヘルパー ---


def _write_transcript(lines: list[dict], path: Path) -> None:
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_user_entry(text: str = "hello") -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _make_assistant_entry(text: str = "hi") -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _make_meta_user_entry() -> dict:
    """isMeta=trueのユーザーエントリ（スキル内容注入等）"""
    return {
        "type": "user",
        "isMeta": True,
        "message": {"content": "skill injection content"},
    }


def _make_tool_result_entry() -> dict:
    """tool_resultを含むユーザーエントリ"""
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_0", "content": "ok"}]},
    }


def _create_mock_claude(tmp_path: Path) -> Path:
    """ダミーのclaudeスクリプトを作成してPATHに配置可能にする。"""
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir()
    mock_claude = mock_bin / "claude"
    mock_claude.write_text("#!/bin/bash\nexit 0\n")
    mock_claude.chmod(mock_claude.stat().st_mode | stat.S_IEXEC)
    return mock_bin


def _run_session_end_hook(
    transcript_path: str,
    env_extra: dict | None = None,
    return_stderr: bool = False,
) -> dict | tuple[dict, str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    input_data = json.dumps({"transcript_path": transcript_path})

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "hooks" / "session_end_hook.py")],
        input=input_data,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=10,
        env=env,
    )

    stdout = result.stdout.strip()
    output = json.loads(stdout) if stdout else {}

    if return_stderr:
        return output, result.stderr
    return output


# --- テスト ---


class TestAlwaysApprove:
    """SessionEnd hookは常にapproveを返す"""

    def test_empty_transcript_path(self):
        input_data = json.dumps({"transcript_path": ""})
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "hooks" / "session_end_hook.py")],
            input=input_data,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=10,
        )
        output = json.loads(result.stdout.strip())
        assert output["decision"] == "approve"

    def test_nonexistent_transcript(self, tmp_path):
        output = _run_session_end_hook(str(tmp_path / "nonexistent.jsonl"))
        assert output["decision"] == "approve"

    def test_invalid_json_input(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "hooks" / "session_end_hook.py")],
            input="not json",
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=10,
        )
        output = json.loads(result.stdout.strip())
        assert output["decision"] == "approve"

    def test_invalid_json_input_records_machine_error_signal(self, temp_db):
        """top-level except到達時にsignal_eventsへmachine_errorが記録される"""
        from src.db import get_connection

        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "hooks" / "session_end_hook.py")],
            input="not json",
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=10,
            env={**os.environ, "DISCUSSION_DB_PATH": temp_db},
        )
        output = json.loads(result.stdout.strip())
        assert output["decision"] == "approve"

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM signal_events WHERE source = 'hook:session_end'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["kind"] == "machine_error"


class TestSyncMarkerCheck:
    """sync-memoryマーカーがあればスキップ"""

    def test_skip_when_marker_present(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("hello"),
            _make_assistant_entry("hi"),
            _make_user_entry("do something"),
            _make_assistant_entry("claude-code-memory:sync-memory done"),
        ], transcript)

        output = _run_session_end_hook(str(transcript))
        assert output["decision"] == "approve"


class TestOneLinerDetection:
    """ワンライナー（user_message_count <= 1）はスキップ"""

    def test_skip_zero_user_messages(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_assistant_entry("system response"),
        ], transcript)

        output = _run_session_end_hook(str(transcript))
        assert output["decision"] == "approve"

    def test_skip_one_user_message(self, tmp_path):
        """パイプモード相当: ユーザーメッセージ1件"""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("do this task"),
            _make_assistant_entry("done"),
        ], transcript)

        output = _run_session_end_hook(str(transcript))
        assert output["decision"] == "approve"

    def test_skip_one_user_message_with_many_assistant(self, tmp_path):
        """ツール多用のワンライナー: user 1件だがassistant多数"""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("complex task"),
            _make_assistant_entry("step 1"),
            _make_tool_result_entry(),
            _make_assistant_entry("step 2"),
            _make_tool_result_entry(),
            _make_assistant_entry("step 3"),
            _make_tool_result_entry(),
            _make_assistant_entry("final answer"),
        ], transcript)

        output = _run_session_end_hook(str(transcript))
        assert output["decision"] == "approve"

    def test_meta_user_entries_not_counted(self, tmp_path):
        """isMeta=trueのエントリはUser Messageとしてカウントしない"""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("only real user message"),
            _make_meta_user_entry(),
            _make_assistant_entry("response"),
            _make_meta_user_entry(),
            _make_assistant_entry("another response"),
        ], transcript)

        output = _run_session_end_hook(str(transcript))
        assert output["decision"] == "approve"

    def test_tool_result_entries_not_counted(self, tmp_path):
        """tool_resultエントリはUser Messageとしてカウントしない"""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("only real message"),
            _make_assistant_entry("using tool"),
            _make_tool_result_entry(),
            _make_assistant_entry("done"),
        ], transcript)

        output = _run_session_end_hook(str(transcript))
        assert output["decision"] == "approve"


class TestAutoSyncLaunch:
    """user_message_count >= 2 かつ sync-memory未実行なら auto-sync を起動"""

    def test_launches_when_multi_turn(self, tmp_path):
        """main()フロー全体をE2Eで検証: モックclaudeで起動確認"""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("first question"),
            _make_assistant_entry("first answer"),
            _make_user_entry("second question"),
            _make_assistant_entry("second answer"),
        ], transcript)

        # モックclaudeをPATH先頭に配置
        mock_bin = _create_mock_claude(tmp_path)

        log_file = tmp_path / "session-end.log"

        # _LOG_FILEとPATHだけ差し替えてmain()を呼ぶラッパー
        wrapper = tmp_path / "run_hook.py"
        wrapper.write_text(f"""\
import sys, os
sys.path.insert(0, {str(PROJECT_ROOT)!r})
os.environ["PATH"] = {str(mock_bin)!r} + os.pathsep + os.environ.get("PATH", "")

import hooks.session_end_hook as hook
from pathlib import Path
hook._LOG_FILE = Path({str(log_file)!r})
hook.main()
""")

        input_data = json.dumps({"transcript_path": str(transcript)})
        result = subprocess.run(
            [sys.executable, str(wrapper)],
            input=input_data,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=10,
        )

        output = json.loads(result.stdout.strip())
        assert output["decision"] == "approve"

        # ログから起動を確認
        log_content = log_file.read_text()
        assert "Launching claude -p" in log_content
        assert "launched in background (pid=" in log_content

    def test_does_not_launch_with_marker(self, tmp_path):
        """マーカーあり + マルチターン → 起動しない"""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("first question"),
            _make_assistant_entry("first answer"),
            _make_user_entry("second question"),
            _make_assistant_entry("claude-code-memory:sync-memory executed"),
        ], transcript)

        log_file = tmp_path / "session-end.log"

        wrapper = tmp_path / "run_hook.py"
        wrapper.write_text(f"""\
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
import hooks.session_end_hook as hook
from pathlib import Path
hook._LOG_FILE = Path({str(log_file)!r})
hook.main()
""")

        input_data = json.dumps({"transcript_path": str(transcript)})
        result = subprocess.run(
            [sys.executable, str(wrapper)],
            input=input_data,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=10,
        )

        output = json.loads(result.stdout.strip())
        assert output["decision"] == "approve"

        log_content = log_file.read_text()
        assert "sync-memory already executed" in log_content
        assert "Launching" not in log_content
