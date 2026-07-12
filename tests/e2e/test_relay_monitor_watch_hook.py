"""hooks/relay_monitor_watch_hook.py (PostToolUse, matcher: Monitor) のE2Eテスト。

subprocess経由でhookを起動し、stdin（tool_name/tool_input/tool_response）を
渡してHookState.monitor_startedマーカーファイルの有無を検証する。
identity解決はresolve_identity_by_ancestryに依存するため、テストプロセス
自身のpidを共通祖先に見立てたlauncher登録ファイルを使う。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.hook_state import HookState
from src.db import get_connection, init_database

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSION_ID = "e2e-relay-monitor-watch-session"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """テスト用のHookState.BASE_DIRを返す"""
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _register_launcher(relay_state_dir: Path) -> str:
    """このテストプロセス自身のpidを共通祖先に見立てたlauncher登録ファイルを作る。

    _run_hookはsubprocess.runでhookを直接の子プロセスとして起動するため、hook
    subprocessのppidは必ずこのテストプロセスのpid（os.getpid()）になる。
    """
    session_id = "relay-monitor-watch-resolved-by-ancestry"
    sessions_dir = relay_state_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    registration = {
        "session_id": session_id,
        "pid": os.getpid(),
        "ancestor_pids": [os.getpid()],
        "created_at": "2026-07-08T00:00:00Z",
    }
    (sessions_dir / f"launcher-{os.getpid()}.json").write_text(
        json.dumps(registration), encoding="utf-8"
    )
    return session_id


def _run_hook(
    input_data: dict, hook_state_dir: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """relay_monitor_watch_hook.pyをサブプロセスで実行する"""
    env = {**os.environ, "HOOK_STATE_DIR": str(hook_state_dir)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "hooks/relay_monitor_watch_hook.py"],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        env=env,
    )


class TestEnvVarGate:
    def test_no_marker_when_env_var_off(self, state_dir, tmp_path):
        """env var OFF（デフォルト）なら、tool_name/commandが完全一致していても
        マーカーを書かない（identity解決自体を試みない）"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = _register_launcher(relay_state_dir)
            path = relay_inbox.inbox_path(identity)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": f"tail -f {path}"},
                "tool_response": {"content": "Monitor started (task xyz)."},
            },
            state_dir,
            extra_env={"RELAY_STATE_DIR": str(relay_state_dir)},
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}
        assert HookState(_SESSION_ID).get_monitor_started() is False


class TestMonitorStartedMarker:
    def test_marker_written_when_command_matches_inbox_path(self, state_dir, tmp_path):
        """env var ON・tool_name=Monitor・commandが該当セッションのinbox pathを
        含む・tool_responseにエラー兆候なしならマーカーを書く"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = _register_launcher(relay_state_dir)
            path = relay_inbox.inbox_path(identity)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": f"tail -f {path}"},
                "tool_response": {"content": "Monitor started (task xyz)."},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}
        assert HookState(_SESSION_ID).get_monitor_started() is True

    def test_no_marker_when_tool_name_is_not_monitor(self, state_dir, tmp_path):
        """matcher対象外のtool（想定外呼び出し・防御的テスト）はマーカーを書かない"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = _register_launcher(relay_state_dir)
            path = relay_inbox.inbox_path(identity)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Bash",
                "tool_input": {"command": f"tail -f {path}"},
                "tool_response": {},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_command_targets_unrelated_path(self, state_dir, tmp_path):
        """コマンドが別ファイルを監視するMonitor呼び出し（このセッションのinbox
        監視ではない）ならマーカーを書かない"""
        relay_state_dir = tmp_path / "relay-state"

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            _register_launcher(relay_state_dir)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": "tail -f /var/log/something-unrelated.log"},
                "tool_response": {},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_identity_unresolved(self, state_dir, tmp_path):
        """launcher登録ファイルが無くidentity解決に失敗する場合はマーカーを
        書かない（fail-open、relay非参加とみなす）"""
        relay_state_dir = tmp_path / "relay-state"

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": "tail -f /whatever/path"},
                "tool_response": {},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_tool_response_reports_error(self, state_dir, tmp_path):
        """tool_responseが明確にis_error=Trueを示すときはマーカーを書かない"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = _register_launcher(relay_state_dir)
            path = relay_inbox.inbox_path(identity)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": f"tail -f {path}"},
                "tool_response": {"is_error": True, "content": "command not found"},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_session_id_empty(self, state_dir, tmp_path):
        relay_state_dir = tmp_path / "relay-state"

        result = _run_hook(
            {
                "session_id": "",
                "tool_name": "Monitor",
                "tool_input": {"command": "tail -f /whatever/path"},
                "tool_response": {},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}


class TestFailOpen:
    """例外系はすべて空JSON出力（フェイルオープン）"""

    def test_invalid_json_input(self, state_dir):
        result = subprocess.run(
            [sys.executable, "hooks/relay_monitor_watch_hook.py"],
            input="not valid json",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={
                **os.environ,
                "HOOK_STATE_DIR": str(state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}
        assert "error" in result.stderr.lower()

    def test_invalid_json_input_records_machine_error_signal(self, state_dir, tmp_path):
        """top-level except到達時にsignal_eventsへmachine_errorが記録される"""
        db_path = str(tmp_path / "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        try:
            init_database()

            result = subprocess.run(
                [sys.executable, "hooks/relay_monitor_watch_hook.py"],
                input="not valid json",
                capture_output=True,
                text=True,
                cwd=str(_PROJECT_ROOT),
                env={
                    **os.environ,
                    "HOOK_STATE_DIR": str(state_dir),
                    "CCM_RELAY_SESSION_AWARE": "1",
                    "DISCUSSION_DB_PATH": db_path,
                },
            )
            assert result.returncode == 0
            assert json.loads(result.stdout) == {}

            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM signal_events WHERE source = 'hook:relay_monitor_watch'"
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            assert row["kind"] == "machine_error"
        finally:
            if "DISCUSSION_DB_PATH" in os.environ:
                del os.environ["DISCUSSION_DB_PATH"]
