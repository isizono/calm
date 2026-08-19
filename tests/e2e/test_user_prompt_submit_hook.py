"""hooks/user_prompt_submit_hook.py のE2Eテスト（イベント駆動アーキテクチャ版）

subprocess.runでuser_prompt_submit_hook.pyを呼び出し、stdin→stdoutの入出力をテスト。
nudge判定はevents.jsonl内のnudgeイベントに基づく。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.hook_state import HookState

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSION_ID = "e2e-test-session-001"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """テスト用のstateディレクトリを返し、HookStateのBASE_DIRもオーバーライド"""
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _run_hook(
    input_data: dict, state_dir: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """user_prompt_submit_hook.pyをサブプロセスで実行する"""
    env = {**os.environ, "HOOK_STATE_DIR": str(state_dir)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "hooks/user_prompt_submit_hook.py"],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        env=env,
    )


def _write_events(events: list[dict], state_dir: Path) -> None:
    """events.jsonlをpre-seedする"""
    state = HookState(_SESSION_ID)
    state.append_events(events)


class TestNoNudge:
    """nudgeイベントなし → 空JSON"""

    def test_empty_json_when_no_events(self, state_dir):
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}

    def test_empty_json_when_no_nudge_events(self, state_dir):
        _write_events(
            [
                {"e": "tool", "name": "get_topics", "turn": 1},
                {"e": "meta", "topic": "test", "turn": 1},
            ],
            state_dir,
        )
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}


class TestRecordNudge:
    """record nudgeイベント → system-reminder注入（hookEventName="UserPromptSubmit"）"""

    def test_record_nudge_injection(self, state_dir):
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "<system-reminder>" in ctx
        assert "直近の応答で記録ツール" in ctx
        assert "add_decisions" in ctx

    def test_nudge_consumed_after_injection(self, state_dir):
        """nudge消費後は空JSON"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        _run_hook({"session_id": _SESSION_ID}, state_dir)

        # 2回目は空JSON
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert json.loads(result.stdout) == {}


class TestRecordNudgeMultiplication:
    """record nudge文言: repeat段階に応じてtierが変わり、実測ターン数(turns_since)が
    文中に埋め込まれる（旧: 同一文言をrepeat回連結する仕様だった）"""

    def test_no_repeat_field_defaults_to_1(self, state_dir):
        """repeatフィールドなし → tier=lowの文言が1回だけ出力され、反復連結は発生しない"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert ctx.count("直近の応答で記録ツール") == 1

    def test_repeat_3_uses_mid_tier_with_turns_since_embedded(self, state_dir):
        """repeat=3 → tier=midの文言が使われ、turns_sinceの実測値が本文に埋め込まれる"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 6, "repeat": 3, "turns_since": 6}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "6ターン記録ツール" in ctx
        assert "経緯が失われつつあります" in ctx
        # tier=lowの文言(旧仕様の単純反復)は混入しない
        assert "該当なしなら無視してOK" not in ctx

    def test_repeat_5_uses_high_tier_with_turns_since_embedded(self, state_dir):
        """repeat=5（上限到達） → tier=highの強い文言が使われ、turns_sinceが埋め込まれる"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 10, "repeat": 5, "turns_since": 10}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "10ターン以上記録ツールが呼ばれていません" in ctx
        assert "セッションの経緯が失われる可能性が高い" in ctx

    def test_turns_since_missing_falls_back_to_repeat_times_two(self, state_dir):
        """turns_sinceフィールドがない旧形式のnudgeイベント（後方互換）でも例外にならず、
        repeat*2の近似値で文言が生成される"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 6, "repeat": 3}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "6ターン記録ツール" in ctx  # repeat(3) * 2 = 6 で近似


class TestFollowUpNudge:
    """follow_up nudgeイベント → system-reminder注入"""

    def test_follow_up_nudge_injection(self, state_dir):
        _write_events(
            [{"e": "nudge", "type": "follow_up", "turn": 3}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "add_decisions" in ctx
        assert "topic" in ctx
        assert "material" in ctx
        assert "tag_notes" in ctx

    def test_follow_up_nudge_takes_priority(self, state_dir):
        """follow_up nudgeが最新なら、record nudgeより優先"""
        _write_events(
            [
                {"e": "nudge", "type": "record", "turn": 2},
                {"e": "nudge", "type": "follow_up", "turn": 3},
            ],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # follow_up nudgeが注入される（最新のnudgeが先に消費される）
        assert "補完すべき記録" in ctx

        # record nudgeはまだ残っている
        result2 = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output2 = json.loads(result2.stdout)
        ctx2 = output2["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx2


class TestEmptySessionId:
    """session_id空 → 空JSON"""

    def test_empty_session_id(self, state_dir):
        result = _run_hook({"session_id": ""}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}

    def test_null_session_id(self, state_dir):
        result = _run_hook({"session_id": None}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}


class TestRelaySessionAwareNudge:
    """relay session-aware毎ターんnudge（CALM_RELAY_SESSION_AWARE=1のときのみ）

    identity解決はresolve_identity_by_ancestry（祖先pidチェーン一致）に依存する。
    _run_hookはsubprocess.runでhookを直接の子プロセスとして起動するため、hook
    subprocessのppidは必ずこのテストプロセスのpid（os.getpid()）になる。
    """

    def _register_launcher(self, state_dir: Path) -> str:
        session_id = "relay-nudge-resolved-by-ancestry"
        sessions_dir = state_dir / "sessions"
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

    def test_no_injection_when_env_var_off(self, state_dir, tmp_path):
        """env var OFF（デフォルト）なら、identity解決可能・未読ありでも何も注入しない"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = self._register_launcher(relay_state_dir)
            relay_inbox.append(identity, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {"session_id": _SESSION_ID},
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
            },
        )
        assert json.loads(result.stdout) == {}

    def test_injection_when_monitor_not_started(self, state_dir, tmp_path):
        """env var ON・identity解決成功・Monitor未起動なら起動指示を注入する"""
        relay_state_dir = tmp_path / "relay-state"

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            self._register_launcher(relay_state_dir)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        result = _run_hook(
            {"session_id": _SESSION_ID},
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "Monitorツール" in ctx
        assert "未読" not in ctx
        # 指摘1: persistent:falseの既定5分timeoutでwatchがサイレントに止まる
        # のを避けるため、起動指示はpersistent:trueの使用を明記する
        assert "persistent: true" in ctx
        # 指摘2: 解決できたidentityはHookStateにキャッシュされる（ps spawn回避）
        assert HookState(_SESSION_ID).get_cached_relay_identity() == "relay-nudge-resolved-by-ancestry"
        # 指摘3: この経路でもensure_inbox_fileを呼び、inbox fileを先行生成する
        # （SessionStart側のtry/exceptで先行touchがスキップされていた場合の
        # フォールバック）
        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            from src.services.relay import inbox as relay_inbox

            assert relay_inbox.inbox_path("relay-nudge-resolved-by-ancestry").exists()
        finally:
            del os.environ["RELAY_STATE_DIR"]

    def test_second_turn_does_not_reresolve_identity_via_ancestry(self, state_dir, tmp_path):
        """指摘2の回帰テスト: 1回目のターンでidentityがキャッシュされたら、
        2回目のターンはlauncher登録ファイルが消えていてもidentity解決済みの
        前提で動作し続ける（=resolve_identity_by_ancestryのps spawnに頼って
        いない）"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = self._register_launcher(relay_state_dir)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        extra_env = {
            "RELAY_STATE_DIR": str(relay_state_dir),
            "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
            "CALM_RELAY_SESSION_AWARE": "1",
        }

        # 1回目: launcher登録ファイルありでidentity解決に成功しキャッシュされる
        result1 = _run_hook({"session_id": _SESSION_ID}, state_dir, extra_env=extra_env)
        assert "Monitorツール" in json.loads(result1.stdout)["hookSpecificOutput"]["additionalContext"]
        assert HookState(_SESSION_ID).get_cached_relay_identity() == identity

        # launcher登録ファイルを削除する（以降resolve_identity_by_ancestryが
        # 呼ばれれば解決できなくなる状況を作る）
        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            for f in (relay_state_dir / "sessions").glob("launcher-*.json"):
                f.unlink()
            relay_inbox.append(identity, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        # 2回目: launcher登録ファイルが無くてもキャッシュ経由でidentityが
        # 解決されるため、未読1件の消化指示が出る（識別子が解決できていなければ
        # 何も注入されないはず）
        result2 = _run_hook({"session_id": _SESSION_ID}, state_dir, extra_env=extra_env)
        ctx2 = json.loads(result2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "relay inbox 未読: 1件" in ctx2

    def test_injection_when_unread_present_and_monitor_started(self, state_dir, tmp_path):
        """env var ON・Monitor起動済み・未読ありなら消化指示のみを注入する（起動指示は出ない）"""
        relay_state_dir = tmp_path / "relay-state"
        from src.services.relay import inbox as relay_inbox

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            identity = self._register_launcher(relay_state_dir)
            relay_inbox.append(identity, {"body": "hello"})
        finally:
            del os.environ["RELAY_STATE_DIR"]

        HookState(_SESSION_ID).set_monitor_started()

        result = _run_hook(
            {"session_id": _SESSION_ID},
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "relay inbox 未読: 1件" in ctx
        assert "relay_receive" in ctx
        assert "Monitorツール" not in ctx

    def test_no_injection_when_monitor_started_and_no_unread(self, state_dir, tmp_path):
        """env var ON・Monitor起動済み・未読0件なら何も注入しない"""
        relay_state_dir = tmp_path / "relay-state"

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            self._register_launcher(relay_state_dir)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        HookState(_SESSION_ID).set_monitor_started()

        result = _run_hook(
            {"session_id": _SESSION_ID},
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert json.loads(result.stdout) == {}

    def test_no_injection_when_identity_unresolved(self, state_dir, tmp_path):
        """env var ON・relay構成済みでもidentity解決に失敗するセッションは
        何も注入しない（fail-open、relay非参加とみなす。launcher登録ファイルを
        一切置かないため、祖先pidチェーンが誰とも一致しない）"""
        relay_state_dir = tmp_path / "relay-state"

        result = _run_hook(
            {"session_id": _SESSION_ID},
            state_dir,
            extra_env={
                "RELAY_STATE_DIR": str(relay_state_dir),
                "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
                "CALM_RELAY_SESSION_AWARE": "1",
            },
        )
        assert json.loads(result.stdout) == {}

    def test_existing_record_nudge_takes_priority(self, state_dir, tmp_path):
        """既存nudge（record等）があればrelay系より優先され、relay系は次ターンに繰り越す"""
        relay_state_dir = tmp_path / "relay-state"

        os.environ["RELAY_STATE_DIR"] = str(relay_state_dir)
        try:
            self._register_launcher(relay_state_dir)
        finally:
            del os.environ["RELAY_STATE_DIR"]

        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)

        extra_env = {
            "RELAY_STATE_DIR": str(relay_state_dir),
            "RELAY_BEARER_TOKEN": "dummy-token-for-e2e",
            "CALM_RELAY_SESSION_AWARE": "1",
        }

        # 1回目: record nudgeが優先される
        result = _run_hook({"session_id": _SESSION_ID}, state_dir, extra_env=extra_env)
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx

        # 2回目: record nudgeは消費済みなのでrelay系が注入される
        result2 = _run_hook({"session_id": _SESSION_ID}, state_dir, extra_env=extra_env)
        ctx2 = json.loads(result2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Monitorツール" in ctx2


class TestEmptyStdin:
    """stdin空/空白のみ → 空JSON、machine_errorシグナルは記録しない"""

    def test_whitespace_only_stdin_returns_empty_json(self, state_dir):
        proc = subprocess.run(
            [sys.executable, "hooks/user_prompt_submit_hook.py"],
            input="   \n\t",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "HOOK_STATE_DIR": str(state_dir)},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}

    def test_empty_stdin_does_not_record_signal(self, state_dir, temp_db):
        """空stdinはjson.loadsの例外経路に入らず、signal_eventsへ記録されない"""
        from src.db import get_connection

        proc = subprocess.run(
            [sys.executable, "hooks/user_prompt_submit_hook.py"],
            input="",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM signal_events WHERE source = 'hook:user_prompt_submit'"
            ).fetchone()
        finally:
            conn.close()
        assert row is None


class TestFailOpen:
    """例外→空JSON（フェイルオープン）"""

    def test_invalid_json_input(self, state_dir, temp_db):
        proc = subprocess.run(
            [sys.executable, "hooks/user_prompt_submit_hook.py"],
            input="not valid json",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}
        assert "error" in proc.stderr.lower()

    def test_invalid_json_input_records_machine_error_signal(self, state_dir, temp_db):
        """top-level except到達時にsignal_eventsへmachine_errorが記録される"""
        from src.db import get_connection

        proc = subprocess.run(
            [sys.executable, "hooks/user_prompt_submit_hook.py"],
            input="not valid json",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM signal_events WHERE source = 'hook:user_prompt_submit'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["kind"] == "machine_error"
