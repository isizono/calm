"""hooks/hook_transcript.py のユニットテスト"""
import json
from pathlib import Path

import pytest

from hooks.hook_transcript import (
    _extract_short_name,
    _is_calm_tool,
    extract_events,
    get_transcript_info,
    has_context_retrieval_calls,
    has_recent_recording,
    is_user_message,
)


# --- ヘルパー ---


def _write_transcript(lines: list[dict], path: Path) -> None:
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_assistant_entry(tool_calls: list[str] | None = None, text: str = "",
                          tool_inputs: list[dict] | None = None) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_calls:
        for i, tool in enumerate(tool_calls):
            inp = tool_inputs[i] if tool_inputs and i < len(tool_inputs) else {}
            content.append({"type": "tool_use", "name": tool, "input": inp})
    return {"type": "assistant", "message": {"content": content}}


def _make_user_entry(text: str = "hello") -> dict:
    return {"type": "human", "message": {"content": [{"type": "text", "text": text}]}}


# --- has_recent_recording ---


class TestHasRecentRecording:
    def test_no_tool_calls(self):
        entries = [_make_assistant_entry(text="hello")]
        assert has_recent_recording(entries) is False

    def test_unrelated_tool_calls(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__search"])]
        assert has_recent_recording(entries) is False

    def test_add_decision_detected(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__add_decisions"])]
        assert has_recent_recording(entries) is True

    def test_add_topic_detected(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__add_topic"])]
        assert has_recent_recording(entries) is True

    def test_mixed_entries(self):
        entries = [
            _make_assistant_entry(text="thinking..."),
            _make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__search"]),
            _make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__add_decisions"]),
        ]
        assert has_recent_recording(entries) is True

    def test_string_content(self):
        entry = {"type": "assistant", "message": {"content": "just a string"}}
        assert has_recent_recording([entry]) is False


# --- has_context_retrieval_calls ---


class TestHasContextRetrievalCalls:
    def test_no_tool_calls(self):
        entries = [_make_assistant_entry(text="hello")]
        assert has_context_retrieval_calls(entries) is False

    def test_get_topics_detected(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__get_topics"])]
        assert has_context_retrieval_calls(entries) is True

    def test_search_detected(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__search"])]
        assert has_context_retrieval_calls(entries) is True

    def test_get_activities_detected(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__get_activities"])]
        assert has_context_retrieval_calls(entries) is True

    def test_get_by_ids_detected(self):
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__get_by_ids"])]
        assert has_context_retrieval_calls(entries) is True

    def test_recording_tool_not_detected(self):
        """記録ツールはコンテキスト取得とみなさない"""
        entries = [_make_assistant_entry(tool_calls=["mcp__plugin_calm_calm__add_decisions"])]
        assert has_context_retrieval_calls(entries) is False


# --- get_transcript_info ---


def _make_user_entry_real(text: str = "hello") -> dict:
    """実際のtranscript形式（type: "user"）でuserエントリを作成する。"""
    return {"type": "user", "message": {"role": "user", "content": text}}


class TestGetTranscriptInfo:
    def test_returns_assistant_entries_and_no_skill(self, tmp_path):
        """通常のtranscript: assistant entriesを返し、skill commandなし"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry_real("hi"),
            _make_assistant_entry(text="response"),
            _make_user_entry_real("more"),
            _make_assistant_entry(text="response2"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert len(entries) == 2
        assert has_skill is False

    def test_detects_skill_command(self, tmp_path):
        """<command-name>を含むuserエントリでskill検出"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry_real(
                '<command-message>sync-memory</command-message>\n'
                '<command-name>/claude-code-memory:sync-memory</command-name>'
            ),
            _make_assistant_entry(text="processing skill"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert len(entries) == 1
        assert has_skill is True

    def test_only_last_user_entry_matters(self, tmp_path):
        """直近のuserエントリのみが判定対象"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry_real(
                '<command-name>/sync-memory</command-name>'
            ),
            _make_assistant_entry(text="skill response"),
            _make_user_entry_real("normal message"),
            _make_assistant_entry(text="normal response"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert len(entries) == 2
        assert has_skill is False

    def test_handles_human_type(self, tmp_path):
        """type: humanのエントリでもskill検出可能"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            {"type": "human", "message": {"content": "<command-name>/test</command-name>"}},
            _make_assistant_entry(text="response"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert has_skill is True

    def test_handles_list_content(self, tmp_path):
        """contentがリスト形式でもskill検出可能"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "<command-name>/test</command-name>"},
            ]}},
            _make_assistant_entry(text="response"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert has_skill is True

    def test_tool_result_does_not_overwrite_skill_detection(self, tmp_path):
        """tool_resultエントリがskill検出を上書きしない"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry_real(
                '<command-message>sync-memory</command-message>\n'
                '<command-name>/claude-code-memory:sync-memory</command-name>'
            ),
            _make_assistant_entry(text="calling tools"),
            # tool_result: type="user"だがskill検出を上書きしてはいけない
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_123", "content": "result"},
            ]}},
            _make_assistant_entry(text="more tools"),
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_456", "content": "result2"},
            ]}},
            _make_assistant_entry(text="final response"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert len(entries) == 3
        assert has_skill is True

    def test_real_user_message_after_tool_result_overrides_skill(self, tmp_path):
        """tool_result後の本物のuserメッセージはskill検出を上書きする"""
        path = tmp_path / "transcript.jsonl"
        _write_transcript([
            _make_user_entry_real(
                '<command-name>/sync-memory</command-name>'
            ),
            _make_assistant_entry(text="skill response"),
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_123", "content": "result"},
            ]}},
            _make_assistant_entry(text="more response"),
            _make_user_entry_real("normal follow-up message"),
            _make_assistant_entry(text="normal response"),
        ], path)
        entries, has_skill = get_transcript_info(str(path))
        assert has_skill is False

    def test_file_not_found(self, tmp_path):
        """ファイルが存在しない場合"""
        path = tmp_path / "nonexistent.jsonl"
        entries, has_skill = get_transcript_info(str(path))
        assert entries == []
        assert has_skill is False

    def test_empty_file(self, tmp_path):
        """空ファイルの場合"""
        path = tmp_path / "transcript.jsonl"
        path.write_text("")
        entries, has_skill = get_transcript_info(str(path))
        assert entries == []
        assert has_skill is False


# --- is_user_message ---


class TestIsUserMessage:
    def test_string_content_is_user_message(self):
        entry = {"type": "user", "message": {"content": "hello"}}
        assert is_user_message(entry) is True

    def test_list_content_without_tool_result_is_user_message(self):
        entry = {"type": "user", "message": {"content": [
            {"type": "text", "text": "hello"},
        ]}}
        assert is_user_message(entry) is True

    def test_tool_result_is_not_user_message(self):
        entry = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_123", "content": "result"},
        ]}}
        assert is_user_message(entry) is False

    def test_assistant_is_not_user_message(self):
        entry = {"type": "assistant", "message": {"content": "hello"}}
        assert is_user_message(entry) is False

    def test_is_meta_entry_is_not_user_message(self):
        """isMeta=trueのエントリ（スキル内容注入等）はUser Messageではない"""
        entry = {
            "type": "user",
            "isMeta": True,
            "message": {"content": [
                {"type": "text", "text": "Base directory for this skill: ..."},
            ]},
        }
        assert is_user_message(entry) is False

    def test_is_meta_false_is_user_message(self):
        """isMeta=falseは通常のUser Message"""
        entry = {
            "type": "user",
            "isMeta": False,
            "message": {"content": "hello"},
        }
        assert is_user_message(entry) is True


# --- extract_events: isMeta handling ---


class TestExtractEventsIsMeta:
    def test_is_meta_does_not_advance_turn(self):
        """isMeta=trueのエントリはturnを進めない"""
        entries = [
            # ユーザーのスキル呼び出し
            {"type": "user", "message": {"content":
                "<command-name>/check-in</command-name>"}},
            # スキル内容注入（isMeta=true）
            {"type": "user", "isMeta": True, "message": {"content": [
                {"type": "text", "text": "Base directory for this skill..."},
            ]}},
            # アシスタント応答
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "response"},
            ]}},
        ]
        events, current_turn = extract_events(entries, 0)
        # turn 1のみ（isMeta=trueでturn 2にならない）
        assert current_turn == 1
        skill_events = [e for e in events if e["e"] == "skill"]
        assert len(skill_events) == 1
        assert skill_events[0]["turn"] == 1


# --- リモートMCPプレフィックス対応テスト ---

_REMOTE_PREFIX = "mcp__claude_ai_calm__"
_LOCAL_PREFIX = "mcp__plugin_calm_calm__"


class TestRemoteMcpPrefix:
    """リモートMCP経由（mcp__claude_ai_calm__*）でもツール検出できることを検証"""

    def test_has_recent_recording_remote(self):
        entries = [_make_assistant_entry(tool_calls=[f"{_REMOTE_PREFIX}add_decisions"])]
        assert has_recent_recording(entries) is True

    def test_has_recent_recording_local(self):
        entries = [_make_assistant_entry(tool_calls=[f"{_LOCAL_PREFIX}add_decisions"])]
        assert has_recent_recording(entries) is True

    def test_has_context_retrieval_remote(self):
        entries = [_make_assistant_entry(tool_calls=[f"{_REMOTE_PREFIX}search"])]
        assert has_context_retrieval_calls(entries) is True

    def test_extract_events_remote_check_in(self):
        """リモートプレフィックスのcheck_inがイベントとして抽出される"""
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(
                tool_calls=[f"{_REMOTE_PREFIX}check_in"],
                tool_inputs=[{"activity_id": 609}],
            ),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool"]
        assert len(tool_events) == 1
        assert tool_events[0]["name"] == "check_in"
        assert tool_events[0]["activity_id"] == 609

    def test_extract_events_remote_add_logs(self):
        """リモートプレフィックスのadd_logsがイベントとして抽出される"""
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(tool_calls=[f"{_REMOTE_PREFIX}add_logs"]),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool"]
        assert len(tool_events) == 1
        assert tool_events[0]["name"] == "add_logs"

    def test_non_calm_tool_ignored(self):
        """CALMでないツールはイベントに含まれない"""
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(tool_calls=["mcp__some_other_tool__search"]),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool"]
        assert len(tool_events) == 0


_LEGACY_REMOTE_PREFIX = "mcp__claude_ai_cc-memory__"
_LEGACY_LOCAL_PREFIX = "mcp__plugin_claude-code-memory_cc-memory__"


class TestLegacyToolNameMarker:
    """改名前のツール名（cc-memory__）を含むtranscriptも解析できることを検証。

    hookは過去に記録されたtranscriptも読むため、新名（calm__）だけを受理すると
    改名前のセッションで記録された呼び出しがすべて検出漏れになる。
    """

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__plugin_calm_calm__search",
            "mcp__claude_ai_calm__search",
            "mcp__plugin_claude-code-memory_cc-memory__search",
            "mcp__claude_ai_cc-memory__search",
            # プラグイン名だけ改名済み・サーバーキーが旧名のままの過渡状態
            "mcp__plugin_calm_cc-memory__search",
        ],
    )
    def test_is_calm_tool_accepts_old_and_new_names(self, tool_name):
        assert _is_calm_tool(tool_name) is True

    @pytest.mark.parametrize(
        "tool_name",
        ["mcp__some_other_tool__search", "Read", "mcp__claude_ai_other__calm"],
    )
    def test_is_calm_tool_rejects_others(self, tool_name):
        assert _is_calm_tool(tool_name) is False

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__plugin_calm_calm__check_in",
            "mcp__claude_ai_calm__check_in",
            "mcp__plugin_claude-code-memory_cc-memory__check_in",
            "mcp__claude_ai_cc-memory__check_in",
            "mcp__plugin_calm_cc-memory__check_in",
        ],
    )
    def test_extract_short_name_from_old_and_new_names(self, tool_name):
        assert _extract_short_name(tool_name) == "check_in"

    def test_extract_short_name_rejects_non_calm_tool(self):
        with pytest.raises(ValueError):
            _extract_short_name("mcp__some_other_tool__search")

    def test_has_recent_recording_legacy_local(self):
        entries = [_make_assistant_entry(tool_calls=[f"{_LEGACY_LOCAL_PREFIX}add_decisions"])]
        assert has_recent_recording(entries) is True

    def test_has_recent_recording_legacy_remote(self):
        entries = [_make_assistant_entry(tool_calls=[f"{_LEGACY_REMOTE_PREFIX}add_decisions"])]
        assert has_recent_recording(entries) is True

    def test_extract_events_legacy_check_in(self):
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(
                tool_calls=[f"{_LEGACY_LOCAL_PREFIX}check_in"],
                tool_inputs=[{"activity_id": 609}],
            ),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool"]
        assert len(tool_events) == 1
        assert tool_events[0]["name"] == "check_in"
        assert tool_events[0]["activity_id"] == 609


class TestExtractAddDecisionsTopicIds:
    """add_decisions のtoolイベントにtopic_idsが付与される"""

    def test_single_item_topic_id(self):
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(
                tool_calls=[f"{_LOCAL_PREFIX}add_decisions"],
                tool_inputs=[{"items": [{"topic_id": 42, "decision": "x", "reason": "y"}]}],
            ),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool" and e["name"] == "add_decisions"]
        assert len(tool_events) == 1
        assert tool_events[0]["topic_ids"] == [42]

    def test_multiple_items_unique_topic_ids_preserved(self):
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(
                tool_calls=[f"{_LOCAL_PREFIX}add_decisions"],
                tool_inputs=[{
                    "items": [
                        {"topic_id": 1, "decision": "a", "reason": "r"},
                        {"topic_id": 2, "decision": "b", "reason": "r"},
                        {"topic_id": 1, "decision": "c", "reason": "r"},
                    ]
                }],
            ),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool" and e["name"] == "add_decisions"]
        assert tool_events[0]["topic_ids"] == [1, 2, 1]

    def test_no_topic_ids_field_when_absent(self):
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(
                tool_calls=[f"{_LOCAL_PREFIX}add_decisions"],
                tool_inputs=[{"items": []}],
            ),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool" and e["name"] == "add_decisions"]
        assert "topic_ids" not in tool_events[0]

    def test_invalid_topic_id_silently_dropped(self):
        entries = [
            {"type": "user", "message": {"content": "hi"}},
            _make_assistant_entry(
                tool_calls=[f"{_LOCAL_PREFIX}add_decisions"],
                tool_inputs=[{
                    "items": [
                        {"topic_id": "not-int", "decision": "x", "reason": "r"},
                        {"topic_id": 7, "decision": "y", "reason": "r"},
                    ]
                }],
            ),
        ]
        events, _ = extract_events(entries, 0)
        tool_events = [e for e in events if e["e"] == "tool" and e["name"] == "add_decisions"]
        assert tool_events[0]["topic_ids"] == [7]
