"""ハートビート機能のユニットテスト

hooks/heartbeat.py, hook_state.py (checked_in_activity), hook_transcript.py (extract_checkin_activity_id)
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from hooks.hook_state import HookState
from hooks.hook_transcript import extract_checkin_activity_id, extract_last_activity_id
from src.db import init_database, get_connection


# ========================================
# hook_state: checked_in_activity
# ========================================


@pytest.fixture
def hook_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return HookState("test-session-hb")


class TestCheckedInActivity:
    def test_get_returns_none_when_no_file(self, hook_state):
        assert hook_state.get_checked_in_activity() is None

    def test_set_then_get(self, hook_state):
        hook_state.set_checked_in_activity(42)
        assert hook_state.get_checked_in_activity() == 42

    def test_overwrite(self, hook_state):
        """別activityにcheck_inしたらHookStateが上書きされる"""
        hook_state.set_checked_in_activity(10)
        hook_state.set_checked_in_activity(20)
        assert hook_state.get_checked_in_activity() == 20

    def test_empty_file_returns_none(self, hook_state):
        path = hook_state._path("checked_in_activity")
        path.write_text("")
        assert hook_state.get_checked_in_activity() is None

    def test_clear_session_clears_checked_in_activity(self, tmp_path, monkeypatch):
        """clear_sessionでchecked_in_activityも削除される"""
        monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
        state = HookState("sess-hb-clear")
        state.set_checked_in_activity(99)

        HookState.clear_session("sess-hb-clear")

        assert state.get_checked_in_activity() is None


# ========================================
# hook_transcript: extract_checkin_activity_id
# ========================================


def _make_assistant_entry(tool_calls=None, text="", tool_inputs=None, tool_ids=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_calls:
        for i, tool in enumerate(tool_calls):
            inp = tool_inputs[i] if tool_inputs and i < len(tool_inputs) else {}
            block = {"type": "tool_use", "name": tool, "input": inp}
            if tool_ids and i < len(tool_ids):
                block["id"] = tool_ids[i]
            content.append(block)
    return {"type": "assistant", "message": {"content": content}}


def _make_tool_result_entry(tool_use_id: str, result_data: dict):
    """tool_resultのuserエントリを生成する"""
    return {
        "type": "human",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": json.dumps(result_data)}],
                }
            ]
        },
    }


_CHECKIN_TOOL = "mcp__plugin_claude-code-memory_cc-memory__check_in"
_ADD_ACTIVITY_TOOL = "mcp__plugin_claude-code-memory_cc-memory__add_activity"
_REMOTE_CHECKIN_TOOL = "mcp__claude_ai_cc-memory__check_in"
_REMOTE_ADD_ACTIVITY_TOOL = "mcp__claude_ai_cc-memory__add_activity"


class TestExtractCheckinActivityId:
    def test_no_entries(self):
        assert extract_checkin_activity_id([]) is None

    def test_no_checkin_calls(self):
        entries = [_make_assistant_entry(text="hello")]
        assert extract_checkin_activity_id(entries) is None

    def test_checkin_with_activity_id(self):
        entries = [
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 42}],
            ),
        ]
        assert extract_checkin_activity_id(entries) == 42

    def test_returns_last_checkin(self):
        """複数check_inがあった場合、最後のactivity_idを返す"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 10}],
            ),
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 20}],
            ),
        ]
        assert extract_checkin_activity_id(entries) == 20

    def test_ignores_add_activity(self):
        """add_activityはcheck_inではないので無視"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_ADD_ACTIVITY_TOOL],
                tool_inputs=[{"title": "new", "description": "desc", "tags": ["domain:test"]}],
            ),
        ]
        assert extract_checkin_activity_id(entries) is None

    def test_checkin_without_activity_id(self):
        """activity_idがない場合はNone"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{}],
            ),
        ]
        assert extract_checkin_activity_id(entries) is None

    def test_activity_id_as_string(self):
        """activity_idが文字列でもint変換される"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": "42"}],
            ),
        ]
        assert extract_checkin_activity_id(entries) == 42

    def test_mixed_tools(self):
        """check_in以外のツールが混在しても正しく動く"""
        entries = [
            _make_assistant_entry(
                tool_calls=["mcp__plugin_claude-code-memory_cc-memory__search"],
                tool_inputs=[{"keyword": "test"}],
            ),
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 55}],
            ),
            _make_assistant_entry(
                tool_calls=["mcp__plugin_claude-code-memory_cc-memory__add_logs"],
                tool_inputs=[{"topic_id": 1, "content": "log"}],
            ),
        ]
        assert extract_checkin_activity_id(entries) == 55

    def test_remote_prefix_checkin(self):
        """リモートMCPプレフィックスのcheck_inも検出できる"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_REMOTE_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 609}],
            ),
        ]
        assert extract_checkin_activity_id(entries) == 609


# ========================================
# hook_transcript: extract_last_activity_id
# ========================================


def _write_transcript(tmp_path, entries: list[dict]) -> str:
    """テスト用transcriptファイルを書き出す"""
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return str(path)


class TestExtractLastActivityId:
    def test_check_in_tool(self, tmp_path):
        """check_inのtool_use inputからactivity_idを取得"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 42}],
                tool_ids=["toolu_1"],
            ),
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) == 42

    def test_add_activity_tool(self, tmp_path):
        """add_activityのtool_resultからactivity_idを取得"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_ADD_ACTIVITY_TOOL],
                tool_inputs=[{"title": "new", "description": "d", "tags": ["domain:t"]}],
                tool_ids=["toolu_add_1"],
            ),
            _make_tool_result_entry("toolu_add_1", {"activity_id": 99, "title": "new"}),
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) == 99

    def test_add_activity_without_matching_result(self, tmp_path):
        """add_activityのtool_resultがない場合はNone"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_ADD_ACTIVITY_TOOL],
                tool_inputs=[{"title": "new", "description": "d", "tags": ["domain:t"]}],
                tool_ids=["toolu_add_2"],
            ),
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) is None

    def test_check_in_overrides_add_activity(self, tmp_path):
        """add_activityの後にcheck_inがあれば、check_inのactivity_idが優先"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_ADD_ACTIVITY_TOOL],
                tool_inputs=[{"title": "a", "description": "d", "tags": ["domain:t"]}],
                tool_ids=["toolu_add_3"],
            ),
            _make_tool_result_entry("toolu_add_3", {"activity_id": 10}),
            _make_assistant_entry(
                tool_calls=[_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 20}],
                tool_ids=["toolu_ci_1"],
            ),
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) == 20

    def test_no_transcript_file(self, tmp_path):
        """transcriptファイルが存在しない場合はNone"""
        assert extract_last_activity_id(str(tmp_path / "nonexistent.jsonl")) is None

    def test_empty_transcript(self, tmp_path):
        """空のtranscriptファイルの場合はNone"""
        path = _write_transcript(tmp_path, [])
        assert extract_last_activity_id(path) is None

    def test_result_content_as_string(self, tmp_path):
        """tool_resultのcontentが文字列（JSON）の場合もパースできる"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_ADD_ACTIVITY_TOOL],
                tool_inputs=[{"title": "a", "description": "d", "tags": ["domain:t"]}],
                tool_ids=["toolu_str_1"],
            ),
            {
                "type": "human",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_str_1",
                            "content": json.dumps({"activity_id": 77}),
                        }
                    ]
                },
            },
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) == 77

    def test_remote_prefix_check_in(self, tmp_path):
        """リモートMCPプレフィックスのcheck_inも検出できる"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_REMOTE_CHECKIN_TOOL],
                tool_inputs=[{"activity_id": 609}],
                tool_ids=["toolu_remote_1"],
            ),
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) == 609

    def test_remote_prefix_add_activity(self, tmp_path):
        """リモートMCPプレフィックスのadd_activityも検出できる"""
        entries = [
            _make_assistant_entry(
                tool_calls=[_REMOTE_ADD_ACTIVITY_TOOL],
                tool_inputs=[{"title": "a", "description": "d", "tags": ["domain:t"]}],
                tool_ids=["toolu_remote_2"],
            ),
            _make_tool_result_entry("toolu_remote_2", {"activity_id": 123}),
        ]
        path = _write_transcript(tmp_path, entries)
        assert extract_last_activity_id(path) == 123


# ========================================
# hooks/heartbeat: update_heartbeat (DB統合)
# ========================================


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestUpdateHeartbeat:
    def test_updates_last_heartbeat_at(self, temp_db):
        """update_heartbeatでlast_heartbeat_atが更新される"""
        from src.services.activity_service import add_activity
        from hooks.heartbeat import update_heartbeat

        result = add_activity(
            title="Heartbeat Test",
            description="Desc",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]

        # 初期状態ではlast_heartbeat_atはNULL
        conn = get_connection()
        row = conn.execute(
            "SELECT last_heartbeat_at FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()
        assert row["last_heartbeat_at"] is None

        # heartbeat更新
        update_heartbeat(activity_id)

        # 更新後はlast_heartbeat_atが設定されている
        conn = get_connection()
        row = conn.execute(
            "SELECT last_heartbeat_at FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()
        assert row["last_heartbeat_at"] is not None

    def test_multiple_updates(self, temp_db):
        """複数回呼び出しても最新の時刻に更新される"""
        from src.services.activity_service import add_activity
        from hooks.heartbeat import update_heartbeat

        result = add_activity(
            title="Heartbeat Test 2",
            description="Desc",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]

        update_heartbeat(activity_id)

        conn = get_connection()
        row1 = conn.execute(
            "SELECT last_heartbeat_at FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()

        update_heartbeat(activity_id)

        conn = get_connection()
        row2 = conn.execute(
            "SELECT last_heartbeat_at FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()

        # 2回目のheartbeatは1回目以降（同一秒の可能性があるので>=）
        assert row2["last_heartbeat_at"] >= row1["last_heartbeat_at"]

    def test_writes_session_id_when_provided(self, temp_db):
        """update_heartbeat に session_id を渡すと last_heartbeat_session_id に保存される"""
        from src.services.activity_service import add_activity
        from hooks.heartbeat import update_heartbeat

        result = add_activity(
            title="Heartbeat session_id Test",
            description="Desc",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]

        update_heartbeat(activity_id, session_id="sess-abc-123")

        conn = get_connection()
        row = conn.execute(
            "SELECT last_heartbeat_at, last_heartbeat_session_id "
            "FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()

        assert row["last_heartbeat_at"] is not None
        assert row["last_heartbeat_session_id"] == "sess-abc-123"

    def test_session_id_defaults_to_null(self, temp_db):
        """session_id を渡さない場合 last_heartbeat_session_id は NULL のまま"""
        from src.services.activity_service import add_activity
        from hooks.heartbeat import update_heartbeat

        result = add_activity(
            title="Heartbeat no-session_id Test",
            description="Desc",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]

        update_heartbeat(activity_id)

        conn = get_connection()
        row = conn.execute(
            "SELECT last_heartbeat_at, last_heartbeat_session_id "
            "FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()

        assert row["last_heartbeat_at"] is not None
        assert row["last_heartbeat_session_id"] is None

    def test_session_id_updated_on_subsequent_heartbeat(self, temp_db):
        """別 session_id で再度 update_heartbeat すると上書きされる"""
        from src.services.activity_service import add_activity
        from hooks.heartbeat import update_heartbeat

        result = add_activity(
            title="Heartbeat session_id Overwrite Test",
            description="Desc",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]

        update_heartbeat(activity_id, session_id="sess-old")
        update_heartbeat(activity_id, session_id="sess-new")

        conn = get_connection()
        row = conn.execute(
            "SELECT last_heartbeat_session_id FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        conn.close()

        assert row["last_heartbeat_session_id"] == "sess-new"
