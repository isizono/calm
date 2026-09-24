"""hooks/stop_hook.py の E2E テスト（イベント駆動アーキテクチャ版）

subprocess.run で stop_hook.py を呼び出し、stdin→stdout の入出力をテスト。
テスト用に tmpdir の state を使う（HOOK_STATE_DIR 環境変数）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.hook_state import HookState
from hooks.recorder_marker import write_marker

# プロジェクトルート
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --- ヘルパー ---


def _write_transcript(lines: list[dict], path: Path) -> None:
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_assistant_entry(
    tool_calls: list[str] | None = None,
    text: str = "",
    tool_inputs: list[dict] | None = None,
) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_calls:
        for i, tool in enumerate(tool_calls):
            inp = tool_inputs[i] if tool_inputs and i < len(tool_inputs) else {}
            content.append({"type": "tool_use", "name": tool, "input": inp, "id": f"tu_{i}"})
    return {"type": "assistant", "message": {"content": content}}


def _make_user_entry(text: str = "hello") -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _make_skill_user_entry(skill_name: str = "sync-memory") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<command-message>{skill_name}</command-message>\n"
                f"<command-name>/{skill_name}</command-name>"
            ),
        },
    }


def _write_events(events: list[dict], state_dir: str, session_id: str) -> None:
    """events.jsonl をpre-seedする"""
    path = Path(state_dir) / f"events_{session_id}.jsonl"
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _read_events(state_dir: str, session_id: str) -> list[dict]:
    """events.jsonl を読み取る"""
    path = Path(state_dir) / f"events_{session_id}.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


CONTEXT_RETRIEVAL_ENTRY = _make_assistant_entry(
    tool_calls=["mcp__plugin_calm_calm__get_topics"],
)


def _run_stop_hook(
    transcript_path: str,
    session_id: str,
    env_override: dict | None = None,
    return_stderr: bool = False,
    agent_type: str | None = None,
) -> dict | tuple[dict, str]:
    payload = {
        "transcript_path": transcript_path,
        "session_id": session_id,
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type
    input_data = json.dumps(payload)

    env = {**os.environ}
    # runnerのOW_ROLEを継承しない（テストの決定性確保。残存env検証テストはenv_overrideで明示設定する）
    env.pop("OW_ROLE", None)
    if env_override:
        env.update(env_override)

    result = subprocess.run(
        [sys.executable, "hooks/stop_hook.py"],
        input=input_data,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    stdout = result.stdout.strip()
    assert stdout, f"stop_hook.py produced no output. stderr: {result.stderr}"
    parsed = json.loads(stdout)

    if return_stderr:
        return parsed, result.stderr
    return parsed


# --- Fixtures ---


@pytest.fixture
def env_setup(tmp_path):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)

    env_override = {
        "HOOK_STATE_DIR": state_dir,
        # 本番DBへ接続しないよう隔離DBを指す。未初期化の空パスのため
        # DB参照を伴う処理は接続/クエリに失敗し、フェイルオープンになる。
        "DISCUSSION_DB_PATH": str(tmp_path / "isolated.db"),
    }

    yield {
        "env_override": env_override,
        "tmp_path": tmp_path,
        "state_dir": state_dir,
    }


# --- テストケース ---


class TestBlockLimitForceApprove:
    """ブロック上限 → force approve"""

    def test_block_limit_force_approves(self, env_setup):
        state_dir = Path(env_setup["state_dir"])

        block_file = state_dir / "block_count_test-session"
        block_file.write_text("2")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(text="no meta tag here"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"]
        )
        assert result["decision"] == "approve"
        assert "ブロック上限" in result["reason"]

        assert not block_file.exists()


class TestExceptionFailOpen:
    """例外発生時 → approve（フェイルオープン）"""

    def test_exception_causes_approve(self, env_setup):
        state_as_file = env_setup["tmp_path"] / "state_as_file"
        state_as_file.write_text("not a directory")

        env_override = {
            **env_setup["env_override"],
            "HOOK_STATE_DIR": str(state_as_file),
        }

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_override,
        )
        assert result["decision"] == "approve"
        assert "error" in result.get("reason", "").lower()

    def test_exception_records_machine_error_signal(self, env_setup, temp_db):
        """top-level except到達時にsignal_eventsへmachine_errorが記録される"""
        from src.db import get_connection

        state_as_file = env_setup["tmp_path"] / "state_as_file_signal"
        state_as_file.write_text("not a directory")

        env_override = {
            "HOOK_STATE_DIR": str(state_as_file),
            "DISCUSSION_DB_PATH": temp_db,
        }

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_override,
        )
        assert result["decision"] == "approve"

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM signal_events WHERE source = 'hook:stop'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["kind"] == "machine_error"

    def test_post_approve_exception_no_double_output(self, env_setup):
        """approve後の状態更新で例外 → stdoutは1行のみ（double-output防止の回帰テスト）"""
        state_dir = env_setup["state_dir"]

        # checked_in_activityを設定 → update_heartbeatが呼ばれる
        Path(state_dir, "checked_in_activity_test-session").write_text("999")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("hi"),
            CONTEXT_RETRIEVAL_ENTRY,
            _make_assistant_entry(text="response"),
        ], transcript)

        # DB pathを空ファイルに向ける → activitiesテーブルがないのでsqlite3.OperationalError
        empty_db = env_setup["tmp_path"] / "empty.db"
        empty_db.touch()

        env = {**os.environ, **env_setup["env_override"], "DISCUSSION_DB_PATH": str(empty_db)}
        input_data = json.dumps({
            "transcript_path": str(transcript),
            "session_id": "test-session",
        })

        result = subprocess.run(
            [sys.executable, "hooks/stop_hook.py"],
            input=input_data,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
        )

        # stdoutは1行のJSONのみ（double-outputなし）
        stdout_lines = result.stdout.strip().split("\n")
        assert len(stdout_lines) == 1, f"Expected 1 stdout line, got {len(stdout_lines)}: {result.stdout}"
        parsed = json.loads(stdout_lines[0])
        assert parsed["decision"] == "approve"

        # stderrにpost-approve errorログが出ている
        assert "post-approve error" in result.stderr


class TestActivityCheckinBlock:
    """activity check-in チェック"""

    def test_no_checkin_after_defer_turns_blocks(self, env_setup):
        """猶予期間後（turn==3）でcheck-in未呼出 → block"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("continue2"),
                _make_assistant_entry(text="response 3"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "block"
        assert "check-in" in result["reason"]

    def test_two_turn_session_never_blocks(self, env_setup):
        """2件のユーザー発言で終わるセッション（軽量セッション）はcheck-in未呼出でもblockされない"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

    def test_checkin_called_approves(self, env_setup):
        """check_in呼出済み → approve"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

    def test_add_activity_called_approves(self, env_setup):
        """add_activity呼出済み → approve"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__add_activity"],
                ),
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

    def test_before_defer_turns_no_block(self, env_setup):
        """猶予期間中（turn<2）ではcheck-in未呼出でもblockしない"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"


class TestRecordingObligationBlock:
    """完了の合図(judge_goal呼び出し)があるのに、記録の基準以降に
    add_logsが無いとき、1セッション1回だけblockする(記録義務block)。

    goalを閉じられるのはjudge_goalだけで、update_goalのsatisfiedは
    goal_conditionsの1件を充足にするだけ(goal本体の完了ではない)ため
    単体では記録義務blockの対象にしない。SendMessageもセッション間の
    中間的な状況共有に使われるため、単体では対象にしない。
    """

    def test_send_message_alone_does_not_block(self, env_setup):
        """SendMessageは完了の合図として扱わない(セッション間の中間的な
        状況共有にも使われ、完了報告と誤検知するため)"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["SendMessage"],
                    tool_inputs=[{"to": "main", "message": "完了しました"}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_update_goal_satisfied_alone_does_not_block(self, env_setup):
        """update_goalのsatisfiedは条件1件の充足に過ぎず、goal本体の完了
        ではないため単体では記録義務blockの対象にしない"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__update_goal"],
                    tool_inputs=[{"goal_id": 1, "changes": [{"op": "set", "id": 5, "state": "satisfied"}]}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_judge_goal_without_logs_since_checkin_blocks(self, env_setup):
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "block"
        assert "add_logs" in result["reason"]

    def test_satisfied_condition_does_not_consume_one_shot_before_real_completion(self, env_setup):
        """条件を1つsatisfiedにしただけの中間更新ではblockせず、1回きりの
        枠も消費しない。その後の本当の完了(judge_goal)ではまだ検査が働く"""
        state_dir = Path(env_setup["state_dir"])
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("partial"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__update_goal"],
                    tool_inputs=[{"goal_id": 1, "changes": [{"op": "set", "id": 5, "state": "satisfied"}]}],
                ),
            ],
            transcript,
        )
        first = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert first["decision"] == "approve"

        # 実運用ではblock_countは次回呼び出しの「ブロック上限」到達で強制
        # approve+リセットされる。ここではそのリセット後の状態を再現する。
        (state_dir / "block_count_test-session").unlink(missing_ok=True)

        with open(transcript, "a") as f:
            f.write(json.dumps(_make_user_entry("done")) + "\n")
            f.write(json.dumps(_make_assistant_entry(
                tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
            )) + "\n")

        second = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert second["decision"] == "block"
        assert "add_logs" in second["reason"]

    def test_judge_goal_without_checkin_and_without_logs_blocks(self, env_setup):
        """check_inを一度も呼んでいなくても、judge_goalはgoal_idを直接指定
        すれば呼べるため、記録義務blockはhas_checkinの有無で素通りさせない"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "block"
        assert "add_logs" in result["reason"]

    def test_judge_goal_without_checkin_but_with_logs_approves(self, env_setup):
        """check_inが無くてもadd_logsさえあれば記録義務blockは発火しない
        (check_inが無いときの基準turnはセッション開始)"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__add_logs", "mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[
                        {"items": [{"topic_id": 1, "content": "経緯"}]},
                        {"goal_id": 1, "verdict": "achieved"},
                    ],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_completion_signal_with_logs_since_checkin_approves(self, env_setup):
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__add_logs", "mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[
                        {"items": [{"topic_id": 1, "content": "経緯"}]},
                        {"goal_id": 1, "verdict": "achieved"},
                    ],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_no_completion_signal_does_not_block(self, env_setup):
        """完了の合図が無ければ、check_in以降add_logsが無くてもblockしない"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("continue"),
                _make_assistant_entry(text="作業中"),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_block_is_one_shot_per_session(self, env_setup):
        """記録義務blockは1セッションにつき1回だけ。block_count(2回連続block
        しないための短期カウンタ)がリセットされた後の次のターンでも、既に
        発火済みなら再度blockしない(専用の永続フラグで担保している)。"""
        state_dir = Path(env_setup["state_dir"])
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
                ),
            ],
            transcript,
        )

        first = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert first["decision"] == "block"

        # 実運用ではblock_countは次回呼び出しの「ブロック上限」到達で強制
        # approve+リセットされる。ここではそのリセット後の状態を再現し、
        # 記録義務フラグ単体が効くかを検証する。
        (state_dir / "block_count_test-session").unlink(missing_ok=True)

        with open(transcript, "a") as f:
            f.write(json.dumps(_make_user_entry("continue")) + "\n")
            f.write(json.dumps(_make_assistant_entry(text="作業継続中")) + "\n")

        second = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert second["decision"] == "approve"

    def test_recorder_attached_session_not_blocked(self, env_setup, monkeypatch):
        """記録役の目印ファイルがあるセッションは、記録の責務が記録役に移っている
        ため、完了の合図とadd_logs不在が揃っても記録義務blockの対象外になる"""
        state_dir = env_setup["state_dir"]
        monkeypatch.setattr(HookState, "BASE_DIR", Path(state_dir))
        write_marker("test-session", os.getpid())

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_recheckin_after_add_logs_does_not_reset_window(self, env_setup):
        """add_logsの後にgoal.nextを読み直すため同じactivityへcheck_inし直しても、
        最初のcheck_in基準で判定するため誤ってblockされない"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("logged"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__add_logs"],
                    tool_inputs=[{"items": [{"topic_id": 1, "content": "経緯"}]}],
                ),
                _make_user_entry("recheck"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

    def test_agent_type_subagent_not_blocked(self, env_setup):
        """サブエージェント発のStop呼び出しは記録義務blockの対象外(状態を一切更新せず即承認)"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__check_in"],
                    tool_inputs=[{"activity_id": 42}],
                ),
                _make_user_entry("done"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__judge_goal"],
                    tool_inputs=[{"goal_id": 1, "verdict": "achieved"}],
                ),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"], agent_type="builder",
        )
        assert result["decision"] == "approve"


class TestSkillSpan:
    """Skill Span中のスキップ機能"""

    def test_skill_span_approves_without_checks(self, env_setup):
        """Skill Span中: メタタグなし・コンテキスト取得なしでもapprove"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_skill_user_entry("sync-memory"),
            _make_assistant_entry(text="processing skill..."),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"
        assert "Skill Span" in result["reason"]

    def test_skill_span_with_is_meta_entry(self, env_setup):
        """スキル内容注入（isMeta=true）がturnを進めずSkill Spanが維持される"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            # スキル呼び出し
            _make_skill_user_entry("check-in"),
            # スキル内容注入（isMeta=true）— turnを進めてはいけない
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": [
                {"type": "text", "text": "Base directory for this skill: ...\n# check-in\n..."},
            ]}},
            _make_assistant_entry(
                tool_calls=["mcp__plugin_calm_calm__get_activities"],
            ),
            _make_assistant_entry(text="activity list here"),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"
        assert "Skill Span" in result["reason"]

    def test_skill_span_continues_on_next_skill_turn(self, env_setup):
        """連続するSkill turnでもSpan継続"""
        state_dir = env_setup["state_dir"]

        _write_events(
            [{"e": "skill", "name": "sync-memory", "turn": 1}],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_skill_user_entry("sync-memory"),
            _make_assistant_entry(text="still processing"),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"
        assert "Skill Span" in result["reason"]

    def test_skill_span_ends_when_no_skill_event(self, env_setup):
        """Skill Span終了: skillイベントがないturnで通常チェック再開"""
        state_dir = env_setup["state_dir"]

        _write_events(
            [
                {"e": "skill", "name": "sync-memory", "turn": 1},
                {"e": "tool", "name": "get_topics", "turn": 1},
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("normal message after skill"),
            _make_assistant_entry(text="response"),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        # Skill Spanが終了し通常チェックが動く → approve（条件を満たしている）
        assert result["decision"] == "approve"
        assert "Skill Span" not in result.get("reason", "")


class TestNudge:
    """nudgeイベントの生成"""

    def test_follow_up_nudge_on_decision_only(self, env_setup):
        """add_decisions呼出あり + 他の記録系/check-in系ツールなし → follow_up nudgeイベント生成"""
        state_dir = env_setup["state_dir"]

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("hi"),
            CONTEXT_RETRIEVAL_ENTRY,
            _make_assistant_entry(
                tool_calls=["mcp__plugin_calm_calm__add_decisions"],
                text="recorded",
            ),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        nudge_events = [e for e in events if e.get("e") == "nudge"]
        follow_up_nudges = [e for e in nudge_events if e.get("type") == "follow_up_after_decision"]
        assert len(follow_up_nudges) >= 1

    def test_no_follow_up_nudge_when_companion_present(self, env_setup):
        """add_decisions + check_in両方呼出 → follow_up nudge不発"""
        state_dir = env_setup["state_dir"]

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("hi"),
            CONTEXT_RETRIEVAL_ENTRY,
            _make_assistant_entry(
                tool_calls=[
                    "mcp__plugin_calm_calm__add_decisions",
                    "mcp__plugin_calm_calm__check_in",
                ],
                tool_inputs=[{}, {"activity_id": 1}],
            ),
            _make_assistant_entry(text="recorded"),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        follow_up_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "follow_up_after_decision"]
        assert len(follow_up_nudges) == 0


class TestRecorderMarkerSuppressesNudges:
    """記録役の目印ファイルがあるセッションではrecord_missing nudgeを抑制する。

    follow_up_after_decision/logs_sparseの抑制も同一ガード（hooks/stop_hook.py::
    _handle_nudges冒頭のearly return）で行われるため、種類ごとの生成ロジックは
    tests/unit/test_stop_hook_recorder_gate.pyで直接検証済み。ここではHOOK_STATE_DIR
    経由の実配線（目印ファイルの実配置・実psコマンドでの生死判定）を確認する。
    """

    def _write_marker(
        self, state_dir: str, session_id: str, pid: int, monkeypatch
    ) -> None:
        """write_marker経由で目印ファイルを実際に書く(実psコマンドを使う)。
        子プロセス(stop_hook.py)が読むstate_dirへ書き込むため、書き込み中だけ
        HookState.BASE_DIRを一時的にそこへ向ける。"""
        monkeypatch.setattr(HookState, "BASE_DIR", Path(state_dir))
        write_marker(session_id, pid)

    def _seed_no_recording_for_4_turns(self, env_setup) -> Path:
        state_dir = env_setup["state_dir"]
        _write_events(
            [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
            ],
            transcript,
        )
        return transcript

    def test_record_nudge_suppressed_when_recorder_pid_alive(self, env_setup, monkeypatch):
        """目印ファイルのpidが生存(このテストプロセス自身)+起動時刻一致 → nudgeが出ない"""
        state_dir = env_setup["state_dir"]
        self._write_marker(state_dir, "test-session", pid=os.getpid(), monkeypatch=monkeypatch)
        transcript = self._seed_no_recording_for_4_turns(env_setup)

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert record_nudges == []

    def test_record_nudge_generated_when_marker_pid_dead(self, env_setup, monkeypatch):
        """目印ファイルはあるがpidが死んでいる → 通常どおりnudgeが出る(フェイルセーフ)"""
        state_dir = env_setup["state_dir"]
        self._write_marker(state_dir, "test-session", pid=999999999, monkeypatch=monkeypatch)
        transcript = self._seed_no_recording_for_4_turns(env_setup)

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1

    def test_record_nudge_generated_when_marker_json_is_broken(self, env_setup):
        """目印ファイルが壊れたJSON → 通常どおりnudgeが出る(フェイルセーフ)"""
        state_dir = env_setup["state_dir"]
        marker_dir = Path(state_dir) / "recorder"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "test-session.json").write_text("{not valid json")
        transcript = self._seed_no_recording_for_4_turns(env_setup)

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1

    def test_checkin_block_unaffected_by_recorder_marker(self, env_setup, monkeypatch):
        """記録役の目印ファイルがあってもcheck-in強制block(a)は普段どおり発火する"""
        state_dir = env_setup["state_dir"]
        self._write_marker(state_dir, "test-session", pid=os.getpid(), monkeypatch=monkeypatch)

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("continue2"),
                _make_assistant_entry(text="response 3"),
            ],
            transcript,
        )

        result = _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])
        assert result["decision"] == "block"
        assert "check-in" in result["reason"]


class TestStateUpdatedOnApprove:
    """approve時の状態更新"""

    def test_block_count_reset_on_approve(self, env_setup):
        """approve後にblock_countが0にリセットされる"""
        state_dir = Path(env_setup["state_dir"])

        block_file = state_dir / "block_count_test-session"
        block_file.write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )

        assert not block_file.exists()

    def test_events_file_created(self, env_setup):
        """初回実行後にevents.jsonlが作成され、toolイベントが記録される"""
        state_dir = env_setup["state_dir"]

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )

        events = _read_events(state_dir, "test-session")
        assert len(events) > 0
        # get_topicsのtoolイベントがある
        tool_events = [e for e in events if e.get("e") == "tool"]
        assert any(e["name"] == "get_topics" for e in tool_events)

    def test_transcript_offset_updated(self, env_setup):
        """approve後にtranscript_offsetがファイルサイズと一致する"""
        state_dir = Path(env_setup["state_dir"])

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )

        offset_file = state_dir / "transcript_offset_test-session"
        assert offset_file.exists()
        offset_val = int(offset_file.read_text().strip())
        assert offset_val == transcript.stat().st_size


def _make_tool_result_entry(tool_use_id: str, content) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]},
    }


class TestAskRegistrationTracking:
    """add_ask/unsubscribe_askの呼び出しがtracked_ask_ids stateへ反映されることを
    確認する（identity解決には一切触れない、Stop hookのtranscriptスキャン経路）。"""

    def test_add_ask_result_is_added_to_tracked_ask_ids(self, env_setup):
        state_dir = Path(env_setup["state_dir"])
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__add_ask"],
                    tool_inputs=[{"question": "q?", "blocks": [1], "tags": ["domain:test"]}],
                ),
                _make_tool_result_entry("tu_0", json.dumps({"id": 42, "deduped": False})),
            ],
            transcript,
        )

        _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])

        tracked_file = state_dir / "tracked_ask_ids_test-session"
        assert tracked_file.exists()
        assert tracked_file.read_text().strip() == "42"

    def test_unsubscribe_ask_removes_from_tracked_ask_ids(self, env_setup):
        state_dir = Path(env_setup["state_dir"])
        tracked_file = state_dir / "tracked_ask_ids_test-session"
        tracked_file.write_text("42\n7\n")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__unsubscribe_ask"],
                    tool_inputs=[{"ask_id": 42}],
                ),
                _make_tool_result_entry("tu_0", json.dumps({"id": 42, "notify_wanted": False})),
            ],
            transcript,
        )

        _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])

        assert tracked_file.read_text().strip() == "7"

    def test_unsubscribe_ask_rejected_by_server_does_not_remove_from_tracked_ask_ids(self, env_setup):
        """unsubscribe_askの呼び出し自体は成立していても、対象askの要求元セッション
        が2件以上でサーバー側がVALIDATION_ERRORを返した場合はnotify_wantedが
        実際には変更されていないため、tracked_ask_idsからも除去してはならない
        （Monitor不調時の保険としてのローカル追跡を誤って失わないため）。"""
        state_dir = Path(env_setup["state_dir"])
        tracked_file = state_dir / "tracked_ask_ids_test-session"
        tracked_file.write_text("42\n7\n")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_calm_calm__unsubscribe_ask"],
                    tool_inputs=[{"ask_id": 42}],
                ),
                _make_tool_result_entry(
                    "tu_0",
                    json.dumps({
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": (
                                "ask id=42 is shared by 2 requester sessions; "
                                "unsubscribe_ask does not support multi-requester asks in this version"
                            ),
                        }
                    }),
                ),
            ],
            transcript,
        )

        _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])

        assert tracked_file.read_text().strip() == "42\n7"

    def test_unrelated_tool_calls_do_not_touch_tracked_ask_ids(self, env_setup):
        """add_ask/unsubscribe_ask以外のツール呼び出しではtracked_ask_ids fileが
        作られない（無関係なStop hook呼び出しのたびにファイルが増殖しない）。"""
        state_dir = Path(env_setup["state_dir"])
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response"),
            ],
            transcript,
        )

        _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])

        assert not (state_dir / "tracked_ask_ids_test-session").exists()


class TestRecordNudgeMultiplication:
    """record nudge増殖: 記録なしターンが続くとrepeatが増える（blockはしない）"""

    def test_4_turns_without_recording_approve_with_nudge(self, env_setup):
        """4ターン記録なし → blockせずapprove、nudgeにrepeat=2"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1
        assert record_nudges[-1]["repeat"] == 2

    def test_4_turns_without_recording_nudge_includes_turns_since(self, env_setup):
        """nudgeイベントにturns_since(経過ターン数の実測値)が保存される"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
            ],
            transcript,
        )

        _run_stop_hook(str(transcript), "test-session", env_setup["env_override"])

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert record_nudges[-1]["turns_since"] == 4
        assert record_nudges[-1]["repeat"] == 2

    def test_2_turns_without_recording_nudge_repeat_1(self, env_setup):
        """2ターン記録なし → nudge repeat=1"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1
        assert record_nudges[-1]["repeat"] == 1

    def test_10_turns_without_recording_nudge_repeat_5_ceiling(self, env_setup):
        """10ターン記録なし → nudge repeat=5（天井）。turns_since//2が5を超えても5で頭打ち"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
                _make_user_entry("turn5"),
                _make_assistant_entry(text="response 5"),
                _make_user_entry("turn6"),
                _make_assistant_entry(text="response 6"),
                _make_user_entry("turn7"),
                _make_assistant_entry(text="response 7"),
                _make_user_entry("turn8"),
                _make_assistant_entry(text="response 8"),
                _make_user_entry("turn9"),
                _make_assistant_entry(text="response 9"),
                _make_user_entry("turn10"),
                _make_assistant_entry(text="response 10"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1
        assert record_nudges[-1]["repeat"] == 5

    def test_6_turns_without_recording_nudge_repeat_3(self, env_setup):
        """6ターン記録なし → nudge repeat=3（6//2=3、天井5には未達）"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
                _make_user_entry("turn5"),
                _make_assistant_entry(text="response 5"),
                _make_user_entry("turn6"),
                _make_assistant_entry(text="response 6"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1
        assert record_nudges[-1]["repeat"] == 3

    def test_recording_resets_nudge_repeat(self, env_setup):
        """記録後はrepeatがリセットされる"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [
                {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1},
                {"e": "tool", "name": "add_decisions", "turn": 3},
            ],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("3")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
                _make_user_entry("turn5"),
                _make_assistant_entry(text="response 5"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
        )
        assert result["decision"] == "approve"


class TestStaleOwRoleEnvIgnored:
    """残存OW_ROLE envの無視（OW_ROLEはコード側で参照されない残骸環境変数）"""

    def test_stale_ow_role_env_still_blocks_checkin(self, env_setup):
        """OW_ROLE=workerが環境に残存していてもcheck-in未呼出のturn==3ではblockする"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("continue2"),
                _make_assistant_entry(text="response 3"),
            ],
            transcript,
        )

        env_override = {**env_setup["env_override"], "OW_ROLE": "worker"}
        result = _run_stop_hook(str(transcript), "test-session", env_override)
        assert result["decision"] == "block"
        assert "check-in" in result["reason"]

    def test_stale_ow_role_env_still_generates_record_nudge(self, env_setup):
        """OW_ROLE=workerが環境に残存していても記録なしターンが続けばrecord nudgeを生成する"""
        state_dir = env_setup["state_dir"]
        _write_events(
            [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
            ],
            transcript,
        )

        env_override = {**env_setup["env_override"], "OW_ROLE": "worker"}
        result = _run_stop_hook(str(transcript), "test-session", env_override)
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1

    def test_normal_activity_still_nudges(self, env_setup, monkeypatch):
        """通常アクティビティでは record nudge を生成する"""
        state_dir = env_setup["state_dir"]
        db_path = str(env_setup["tmp_path"] / "normal.db")
        import src.config
        from src.db import init_database, get_connection
        monkeypatch.setenv("DISCUSSION_DB_PATH", db_path)
        monkeypatch.setattr(src.config, "DB_PATH", db_path)
        init_database()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activities (id, title, description, status) VALUES (?, ?, ?, ?)",
                (1, "[作業] 個人タスク", "desc", "in_progress"),
            )
            conn.commit()
        finally:
            conn.close()

        _write_events(
            [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}],
            state_dir, "test-session",
        )
        Path(state_dir, "current_turn_test-session").write_text("1")
        Path(state_dir, "checked_in_activity_test-session").write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("turn2"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("turn3"),
                _make_assistant_entry(text="response 3"),
                _make_user_entry("turn4"),
                _make_assistant_entry(text="response 4"),
            ],
            transcript,
        )

        env_override = {**env_setup["env_override"], "DISCUSSION_DB_PATH": db_path}
        env_override.pop("OW_ROLE", None)
        result = _run_stop_hook(str(transcript), "test-session", env_override)
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) >= 1


class TestSubagentStopSkipped:
    """agent_type付き（サブエージェント発）のStop呼び出しは状態を一切更新せず即承認する"""

    def test_agent_type_preserves_block_count(self, env_setup):
        """block_count=1が事前にある状態でagent_type付き呼び出し → block_countは変化しない

        修正前はブロック上限チェックでblock_countが0にリセットされてしまい、
        親セッションのone-shot block（turn 3で1回だけblockする仕組み）が
        再度発火してしまっていた。
        """
        state_dir = Path(env_setup["state_dir"])
        block_file = state_dir / "block_count_test-session"
        block_file.write_text("1")

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([_make_user_entry("hi")], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
            agent_type="builder",
        )
        assert result["decision"] == "approve"
        assert block_file.read_text() == "1"

    def test_agent_type_skips_checkin_block(self, env_setup):
        """check-in未呼出でturn==3相当のtranscriptでも、agent_type付きならblockされない

        加えてtranscript_offset/current_turnのstateファイルも作られない
        （サブエージェントのターンが親のturn数を進めない）。
        """
        state_dir = Path(env_setup["state_dir"])
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(text="response 1"),
                _make_user_entry("continue"),
                _make_assistant_entry(text="response 2"),
                _make_user_entry("continue2"),
                _make_assistant_entry(text="response 3"),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
            agent_type="builder",
        )
        assert result["decision"] == "approve"
        assert not (state_dir / "transcript_offset_test-session").exists()
        assert not (state_dir / "current_turn_test-session").exists()

    def test_agent_type_skips_nudge(self, env_setup):
        """add_decisions単発の呼び出しでも、agent_type付きならfollow_upナッジが出ない"""
        state_dir = env_setup["state_dir"]

        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript([
            _make_user_entry("hi"),
            CONTEXT_RETRIEVAL_ENTRY,
            _make_assistant_entry(
                tool_calls=["mcp__plugin_calm_calm__add_decisions"],
                text="recorded",
            ),
        ], transcript)

        result = _run_stop_hook(
            str(transcript), "test-session", env_setup["env_override"],
            agent_type="builder",
        )
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        assert events == []


# --- Codexハーネス（CALM_HARNESS=codex + rollout形式transcript） ---


def _rollout_user(text: str, ordinal: int) -> dict:
    return {
        "timestamp": "t",
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "UserMessage",
                "id": f"item_u{ordinal}",
                "content": [{"type": "text", "text": text}],
            },
        },
    }


def _rollout_mcp_check_in(activity_id: int, ordinal: int) -> dict:
    return {
        "timestamp": "t",
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "McpToolCall",
                "id": f"exec-{ordinal}",
                "server": "calm",
                "tool": "check_in",
                "arguments": {"activity_id": activity_id},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "{}"}]},
            },
        },
    }


def _rollout_injected_user(ordinal: int) -> dict:
    """Codexが注入するrole=userのresponse_item（実発話ではない）。"""
    return {
        "timestamp": "t",
        "ordinal": ordinal,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context/>"}],
        },
    }


class TestCodexHarness:
    """CALM_HARNESS=codexでrollout形式transcriptを処理する経路のE2E。"""

    def _codex_env(self, env_setup) -> dict:
        return {**env_setup["env_override"], "CALM_HARNESS": "codex"}

    def test_checkin無しでdefer_turn到達時はblockされる(self, env_setup):
        transcript = env_setup["tmp_path"] / "rollout.jsonl"
        _write_transcript(
            [
                _rollout_injected_user(0),
                _rollout_user("1", 1),
                _rollout_user("2", 2),
                _rollout_user("3", 3),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "codex-session-block", self._codex_env(env_setup)
        )

        assert result["decision"] == "block"
        assert "check-in" in result["reason"]

    def test_checkin済みならapproveは空応答になる(self, env_setup):
        """Codexのapproveは空JSON（decision:"approve"はCodexが受理しない）。"""
        transcript = env_setup["tmp_path"] / "rollout.jsonl"
        _write_transcript(
            [
                _rollout_user("1", 1),
                _rollout_mcp_check_in(activity_id=42, ordinal=2),
                _rollout_user("2", 3),
                _rollout_user("3", 4),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "codex-session-checkin", self._codex_env(env_setup)
        )

        assert result == {}

        # check_inのactivity_idがstateへ保存されている
        state_file = Path(env_setup["state_dir"]) / "checked_in_activity_codex-session-checkin"
        assert state_file.read_text().strip() == "42"

    def test_注入userはturnを進めない(self, env_setup):
        """機械注入のresponse_itemだけではdefer turnに到達しない。"""
        transcript = env_setup["tmp_path"] / "rollout.jsonl"
        _write_transcript(
            [
                _rollout_injected_user(0),
                _rollout_injected_user(1),
                _rollout_user("1", 2),
                _rollout_injected_user(3),
                _rollout_user("2", 4),
            ],
            transcript,
        )

        result = _run_stop_hook(
            str(transcript), "codex-session-meta", self._codex_env(env_setup)
        )

        # 実発話2ターンのみ → defer turn(3)未到達でapprove（空応答）
        assert result == {}
