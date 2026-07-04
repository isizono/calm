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
    tool_calls=["mcp__plugin_claude-code-memory_cc-memory__get_topics"],
)


def _run_stop_hook(
    transcript_path: str,
    session_id: str,
    env_override: dict | None = None,
    return_stderr: bool = False,
) -> dict | tuple[dict, str]:
    input_data = json.dumps({
        "transcript_path": transcript_path,
        "session_id": session_id,
    })

    env = {**os.environ}
    # runnerのOW_ROLEを継承しない（テストの決定性確保。worker抑制テストはenv_overrideで明示設定する）
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
        # orch-managed判定が本番DBへ接続しないよう隔離DBを指す。
        # 未初期化の空パスのため接続/クエリは失敗し、フェイルオープン（False）になる。
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
        """猶予期間後（turn>=2）でcheck-in未呼出 → block"""
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
        assert result["decision"] == "block"
        assert "check-in" in result["reason"]

    def test_checkin_called_approves(self, env_setup):
        """check_in呼出済み → approve"""
        transcript = env_setup["tmp_path"] / "transcript.jsonl"
        _write_transcript(
            [
                _make_user_entry("hi"),
                CONTEXT_RETRIEVAL_ENTRY,
                _make_assistant_entry(
                    tool_calls=["mcp__plugin_claude-code-memory_cc-memory__check_in"],
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
                    tool_calls=["mcp__plugin_claude-code-memory_cc-memory__add_activity"],
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
                tool_calls=["mcp__plugin_claude-code-memory_cc-memory__get_activities"],
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
                tool_calls=["mcp__plugin_claude-code-memory_cc-memory__add_decisions"],
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
                    "mcp__plugin_claude-code-memory_cc-memory__add_decisions",
                    "mcp__plugin_claude-code-memory_cc-memory__check_in",
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


def _seed_orch_managed_db(db_path: str, activity_id: int, monkeypatch) -> None:
    """テスト用DBを初期化し、orch_managed=1 のアクティビティを作成する"""
    import src.config
    from src.db import init_database, get_connection

    monkeypatch.setenv("DISCUSSION_DB_PATH", db_path)
    monkeypatch.setattr(src.config, "DB_PATH", db_path)
    init_database()

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activities (id, title, description, status, orch_managed) "
            "VALUES (?, ?, ?, ?, ?)",
            (activity_id, "[作業] orch管理タスク", "desc", "in_progress", 1),
        )
        conn.commit()
    finally:
        conn.close()


class TestOrchFlowSuppression:
    """orchフロー（worker セッション・orch_managed=1 アクティビティ）でのcheck-inブロック/nudge抑制"""

    def test_worker_session_no_checkin_block(self, env_setup):
        """OW_ROLE=worker時はcheck-in未呼出でもturn>=2でblockしない"""
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

        env_override = {**env_setup["env_override"], "OW_ROLE": "worker"}
        result = _run_stop_hook(str(transcript), "test-session", env_override)
        assert result["decision"] == "approve"

    def test_worker_session_no_record_nudge(self, env_setup):
        """OW_ROLE=worker時は記録なしターンが続いてもrecord nudgeを生成しない"""
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
        assert len(record_nudges) == 0

    def test_orch_managed_activity_no_record_nudge(self, env_setup, monkeypatch):
        """orch_managed=1 アクティビティにcheck-in済みなら記録なしでもrecord nudgeを生成しない"""
        state_dir = env_setup["state_dir"]
        db_path = str(env_setup["tmp_path"] / "orch.db")
        _seed_orch_managed_db(db_path, activity_id=1, monkeypatch=monkeypatch)

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
        # OW_ROLEは設定しない（orch_managed=1 カラムのみで抑制されることを確認）
        env_override.pop("OW_ROLE", None)
        result = _run_stop_hook(str(transcript), "test-session", env_override)
        assert result["decision"] == "approve"

        events = _read_events(state_dir, "test-session")
        record_nudges = [e for e in events if e.get("e") == "nudge" and e.get("type") == "record_missing"]
        assert len(record_nudges) == 0

    def test_normal_activity_still_nudges(self, env_setup, monkeypatch):
        """orch_managed=0 の通常アクティビティでは record nudge を生成する"""
        state_dir = env_setup["state_dir"]
        db_path = str(env_setup["tmp_path"] / "normal.db")
        # 通常アクティビティ（orch_managed=0、デフォルト）のDBを作る
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
