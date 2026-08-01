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
        """env var ON・tool_name=Monitor・persistent:true・commandが該当
        セッションのinbox pathを含む・tool_responseにエラー兆候なしなら
        マーカーを書く"""
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
                "tool_input": {"command": f"tail -f {path}", "persistent": True},
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
                "tool_input": {"command": f"tail -f {path}", "persistent": True},
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
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_command_targets_unrelated_path(self, state_dir, tmp_path):
        """コマンドが別ファイルを監視するMonitor呼び出し（このセッションのinbox
        監視ではない）ならマーカーを書かない。persistent:trueを付けた状態でも
        path不一致でマーカーが立たないことを確認する（persistentチェックと
        path一致チェックの分岐を取り違えていないことの確認）"""
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
                "tool_input": {
                    "command": "tail -f /var/log/something-unrelated.log",
                    "persistent": True,
                },
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
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_identity_unresolved(self, state_dir, tmp_path):
        """launcher登録ファイルが無くidentity解決に失敗する場合はマーカーを
        書かない（fail-open、relay非参加とみなす）。persistent:trueを付けた
        状態でもidentity未解決でマーカーが立たないことを確認する"""
        relay_state_dir = tmp_path / "relay-state"

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": "tail -f /whatever/path", "persistent": True},
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
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_tool_response_reports_error(self, state_dir, tmp_path):
        """tool_responseが明確にis_error=Trueを示すときはマーカーを書かない。
        persistent:trueを付けた状態でもtool_responseのエラーでマーカーが
        立たないことを確認する"""
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
                "tool_input": {"command": f"tail -f {path}", "persistent": True},
                "tool_response": {"is_error": True, "content": "command not found"},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}
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


class TestPersistentRequired:
    """persistent:trueで呼ばれた場合のみマーカーを書く。

    persistent:false（既定）はMonitorがtimeout_ms既定値（5分）で自動終了する
    ため、マーカーだけ立てても監視の生存を保証できない。マーカーが一度立つと
    以降のターンでは「起動済み」と判定され続けリマインダーが出なくなるため、
    実際にpersistent:trueで起動された場合にのみマーカーを書く。
    """

    def test_no_marker_when_persistent_is_false(self, state_dir, tmp_path):
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
                "tool_input": {"command": f"tail -f {path}", "persistent": False},
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
        assert HookState(_SESSION_ID).get_monitor_started() is False

    def test_no_marker_when_persistent_is_missing(self, state_dir, tmp_path):
        """persistentキー自体が省略された場合（Monitorツールの既定値相当）も
        マーカーを書かない"""
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
        assert HookState(_SESSION_ID).get_monitor_started() is False


class TestIdentityCacheSharing:
    """identity解決結果をHookState.relay_identityで読み書きし、
    user_prompt_submit_hookと双方向で共有する。"""

    def test_resolved_identity_is_cached(self, state_dir, tmp_path):
        """このhookが解決したidentityはHookStateにキャッシュされ、
        user_prompt_submit_hook側の初回解決でも再利用できる"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = _register_launcher(relay_state_dir)
            path = relay_inbox.inbox_path(identity)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": f"tail -f {path}", "persistent": True},
                "tool_response": {"content": "Monitor started (task xyz)."},
            },
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert HookState(_SESSION_ID).get_cached_relay_identity() == identity

    def test_uses_pre_cached_identity_without_resolving_again(self, state_dir, tmp_path):
        """事前にHookStateへidentityがキャッシュされていれば（例えば
        user_prompt_submit_hookが先に解決していた場合）、launcher登録ファイルが
        存在しなくてもそのキャッシュを使ってマーカーを書ける（ps spawnを
        経由しない）"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        HookState(_SESSION_ID).set_cached_relay_identity("pre-cached-identity")
        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            path = relay_inbox.inbox_path("pre-cached-identity")
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {
                "session_id": _SESSION_ID,
                "tool_name": "Monitor",
                "tool_input": {"command": f"tail -f {path}", "persistent": True},
                "tool_response": {"content": "Monitor started (task xyz)."},
            },
            state_dir,
            # launcher登録ファイルは作らない（resolve_identity_by_ancestryが
            # 呼ばれれば必ず解決失敗する状況）
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "CCM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}
        assert HookState(_SESSION_ID).get_monitor_started() is True


class TestFailOpen:
    """例外系はすべて空JSON出力（フェイルオープン）"""

    def test_invalid_json_input(self, state_dir, tmp_path):
        db_path = str(tmp_path / "test.db")
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
