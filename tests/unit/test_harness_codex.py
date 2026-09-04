"""CodexHarness（src/harness/codex.py）とselect_harnessの契約テスト。

Codexのhookプロトコルとの差分（Stop応答のapprove表現・未サポート系統）と、
rollout形式transcriptの中間表現への正規化を契約として検証する。
rolloutのフィクスチャ構造はCodex CLI 0.149.0の実rolloutファイルから
採取した形（`{timestamp, ordinal, type, payload}` / item_completedの
UserMessage・AgentMessage・McpToolCall）に基づく。
"""
import io
import json

from hooks.hook_transcript import extract_events, extract_last_activity_id
from src.harness import ClaudeCodeHarness, CodexHarness, select_harness


def _make(hook_event_name: str | None = None):
    stdout = io.StringIO()
    harness = CodexHarness(
        hook_event_name=hook_event_name, stdin=io.StringIO(""), stdout=stdout
    )
    return harness, stdout


# ---------------------------------------------------------------------------
# rolloutフィクスチャ
# ---------------------------------------------------------------------------


def _rollout_item(item: dict, ordinal: int = 0) -> dict:
    return {
        "timestamp": "2026-08-25T14:14:22.000Z",
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": item},
    }


def _user_item(text: str) -> dict:
    return {"type": "UserMessage", "id": "item_u", "content": [{"type": "text", "text": text}]}


def _agent_item(text: str) -> dict:
    return {"type": "AgentMessage", "id": "item_a", "content": [{"type": "text", "text": text}]}


def _mcp_item(server: str, tool: str, arguments, result=None, call_id: str = "exec-1") -> dict:
    return {
        "type": "McpToolCall",
        "id": call_id,
        "server": server,
        "tool": tool,
        "arguments": arguments,
        "status": "completed",
        "result": result,
    }


def _injected_user_response_item() -> dict:
    """rolloutのresponse_itemに現れる機械注入のuser roleエントリ。

    Codexは環境コンテキスト等をrole=userのresponse_itemとして注入するが、
    item_completed側にUserMessage itemは生成されない。turn境界の誤検出を
    防ぐため、正規化はこれをuser扱いしてはならない。
    """
    return {
        "timestamp": "2026-08-25T14:14:22.000Z",
        "ordinal": 5,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context>...</environment_context>"}],
        },
    }


def _write_jsonl(path, entries):
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. hook入出力の差分
# ---------------------------------------------------------------------------


class TestEmitDiffs:
    def test_approveは空応答で出力しreasonを載せない(self):
        harness, stdout = _make()
        harness.emit_approve("ブロック上限に達しました")
        assert json.loads(stdout.getvalue()) == {}

    def test_blockはClaude_Codeと同形式(self):
        harness, stdout = _make()
        harness.emit_block("check_inしてください")
        assert json.loads(stdout.getvalue()) == {
            "decision": "block",
            "reason": "check_inしてください",
        }

    def test_updated_tool_outputは何も出力せずFalse(self):
        harness, stdout = _make(hook_event_name="PostToolUse")
        assert harness.emit_updated_tool_output({"content": []}) is False
        assert stdout.getvalue() == ""

    def test_display_contentは何も出力せずFalse(self):
        harness, stdout = _make(hook_event_name="MessageDisplay")
        assert harness.emit_display_content("M#1") is False
        assert stdout.getvalue() == ""

    def test_monitor_watchをサポートしない(self):
        harness, _ = _make()
        assert harness.supports_monitor_watch is False

    def test_additional_contextは継承したhookSpecificOutput形式(self):
        harness, stdout = _make(hook_event_name="SessionStart")
        harness.emit_additional_context("文脈")
        assert json.loads(stdout.getvalue()) == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "文脈",
            }
        }


# ---------------------------------------------------------------------------
# 2. rollout正規化
# ---------------------------------------------------------------------------


class TestRolloutNormalization:
    def test_item_completedの3種を正規化する(self, tmp_path):
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(
            transcript,
            [
                {"timestamp": "t", "ordinal": 0, "type": "session_meta", "payload": {"id": "s"}},
                _rollout_item(_user_item("こんにちは"), 1),
                _rollout_item(_agent_item("応答します"), 2),
                _rollout_item(
                    _mcp_item("calm", "check_in", {"activity_id": 7}, call_id="exec-9"), 3
                ),
            ],
        )
        harness = CodexHarness()

        entries = harness.read_transcript_entries(str(transcript))

        assert [e.kind for e in entries] == ["other", "user", "assistant", "assistant"]
        assert entries[1].content == [{"type": "text", "text": "こんにちは"}]
        assert entries[2].content == [{"type": "text", "text": "応答します"}]
        # McpToolCallはClaude形式のtool_use + tool_result blockに合成される
        assert entries[3].content[0] == {
            "type": "tool_use",
            "id": "exec-9",
            "name": "mcp__calm__check_in",
            "input": {"activity_id": 7},
        }
        assert entries[3].content[1]["type"] == "tool_result"
        assert entries[3].content[1]["tool_use_id"] == "exec-9"
        # rawはrollout行の無加工
        assert entries[1].raw["type"] == "event_msg"

    def test_機械注入のuser_response_itemはuser扱いしない(self, tmp_path):
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(
            transcript,
            [
                _injected_user_response_item(),
                _rollout_item(_user_item("実プロンプト"), 9),
            ],
        )
        harness = CodexHarness()

        entries = harness.read_transcript_entries(str(transcript))

        assert [e.kind for e in entries] == ["other", "user"]

    def test_arguments文字列はdictへパースする(self, tmp_path):
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(
            transcript,
            [_rollout_item(_mcp_item("calm", "check_in", '{"activity_id": 3}'))],
        )
        harness = CodexHarness()

        entries = harness.read_transcript_entries(str(transcript))

        assert entries[0].content[0]["input"] == {"activity_id": 3}

    def test_mcp_resultのcontentはtool_resultへ引き継がれる(self, tmp_path):
        result = {"content": [{"type": "text", "text": '{"activity_id": 42}'}]}
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(
            transcript,
            [_rollout_item(_mcp_item("calm", "add_activity", {"title": "t"}, result))],
        )
        harness = CodexHarness()

        entries = harness.read_transcript_entries(str(transcript))

        assert entries[0].content[1]["content"] == [
            {"type": "text", "text": '{"activity_id": 42}'}
        ]

    def test_rewrite_transcript_entryは未サポートでFalse(self, tmp_path):
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(transcript, [_rollout_item(_user_item("a"))])
        before = transcript.read_text(encoding="utf-8")
        harness = CodexHarness()
        entry = harness.read_transcript_entries(str(transcript))[0]

        assert harness.rewrite_transcript_entry(str(transcript), entry) is False
        assert transcript.read_text(encoding="utf-8") == before

    def test_resolve_session_identityはNone(self):
        assert CodexHarness().resolve_session_identity() is None


# ---------------------------------------------------------------------------
# 3. extract_eventsとの結合（turn計数・calmツール検出）
# ---------------------------------------------------------------------------


class TestRolloutEventExtraction:
    def test_turn計数とcheck_in検出がrolloutで機能する(self, tmp_path):
        result = {"content": [{"type": "text", "text": '{"status": "ok"}'}]}
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(
            transcript,
            [
                {"timestamp": "t", "ordinal": 0, "type": "session_meta", "payload": {}},
                _injected_user_response_item(),
                _rollout_item(_user_item("1ターン目"), 1),
                _rollout_item(
                    _mcp_item("calm", "check_in", {"activity_id": 7}, result), 2
                ),
                _rollout_item(_agent_item("done"), 3),
                _rollout_item(_user_item("2ターン目"), 4),
            ],
        )
        harness = CodexHarness()
        entries = harness.read_transcript_entries(str(transcript))

        events, current_turn = extract_events(entries, 0)

        # 機械注入はturnを進めない
        assert current_turn == 2
        tool_events = [e for e in events if e["e"] == "tool"]
        assert tool_events == [
            {"e": "tool", "name": "check_in", "turn": 1, "activity_id": 7}
        ]

    def test_add_activityのresultからactivity_idを抽出できる(self, tmp_path):
        result = {"content": [{"type": "text", "text": '{"activity_id": 99}'}]}
        transcript = tmp_path / "rollout.jsonl"
        _write_jsonl(
            transcript,
            [
                _rollout_item(_user_item("作って"), 0),
                _rollout_item(
                    _mcp_item("calm", "add_activity", {"title": "新規"}, result), 1
                ),
            ],
        )
        harness = CodexHarness()
        entries = harness.read_transcript_entries(str(transcript))

        assert extract_last_activity_id(entries) == 99


# ---------------------------------------------------------------------------
# 4. select_harness
# ---------------------------------------------------------------------------


class TestSelectHarness:
    def test_env未設定はClaudeCodeHarness(self, monkeypatch):
        for name in ("CALM_HARNESS", "CCM_HARNESS", "CC_MEMORY_HARNESS"):
            monkeypatch.delenv(name, raising=False)
        harness = select_harness(hook_event_name="Stop")
        assert type(harness) is ClaudeCodeHarness

    def test_codex指定はCodexHarness(self, monkeypatch):
        monkeypatch.setenv("CALM_HARNESS", "codex")
        harness = select_harness(hook_event_name="Stop")
        assert type(harness) is CodexHarness

    def test_未知値はClaudeCodeHarnessにフォールバック(self, monkeypatch):
        monkeypatch.setenv("CALM_HARNESS", "unknown-harness")
        harness = select_harness()
        assert type(harness) is ClaudeCodeHarness
