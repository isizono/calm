"""ClaudeCodeHarness（src/harness/claude_code.py）の契約テスト。

Harnessインターフェースは既存hookのClaude Code依存箇所を置き換える土台
であり、ここでは「hookプロトコルの入出力形式」
「transcript中間表現への正規化」「エントリ書き戻し」を契約として検証する。
identity解決（resolve_session_identity）はps spawn・launcher登録ファイル
という外部境界に依存するため、配線はrelay identity側のテストに委ねる。
"""
import io
import json

import pytest

from src.harness import ClaudeCodeHarness, TranscriptEntry


def _make(stdin_text: str = "", hook_event_name: str | None = None):
    """テスト用にストリームを注入したharnessと出力バッファを返す。"""
    stdout = io.StringIO()
    harness = ClaudeCodeHarness(
        hook_event_name=hook_event_name,
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
    )
    return harness, stdout


def _emitted(stdout: io.StringIO) -> dict:
    text = stdout.getvalue()
    assert text.endswith("\n"), "応答は改行終端の1行で出力される"
    return json.loads(text)


# ---------------------------------------------------------------------------
# 1. hook入出力
# ---------------------------------------------------------------------------


class TestReadHookInput:
    def test_有効なJSON_dictをそのまま返す(self):
        payload = {"session_id": "abc", "transcript_path": "/tmp/t.jsonl"}
        harness, _ = _make(json.dumps(payload))
        assert harness.read_hook_input() == payload

    def test_空入力は空dictを返す(self):
        harness, _ = _make("   \n")
        assert harness.read_hook_input() == {}

    def test_トップレベルがdict以外なら空dictを返す(self):
        harness, _ = _make("[1, 2]")
        assert harness.read_hook_input() == {}

    def test_壊れたJSONは例外を送出し呼び出し側が方針を決める(self):
        harness, _ = _make("{not json")
        with pytest.raises(json.JSONDecodeError):
            harness.read_hook_input()


class TestEmit:
    def test_additional_contextはhookEventName付きで出力される(self):
        harness, stdout = _make(hook_event_name="SessionStart")
        harness.emit_additional_context("文脈テキスト")
        assert _emitted(stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "文脈テキスト",
            }
        }

    def test_additional_contextはevent名未設定だとValueError(self):
        harness, stdout = _make()
        with pytest.raises(ValueError):
            harness.emit_additional_context("x")
        assert stdout.getvalue() == "", "エラー時は応答を出力しない"

    def test_permission_decision(self):
        harness, stdout = _make(hook_event_name="PreToolUse")
        harness.emit_permission_decision("deny", "internal ID leak")
        assert _emitted(stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "internal ID leak",
            }
        }

    def test_block(self):
        harness, stdout = _make()
        harness.emit_block("check_inしてください")
        assert _emitted(stdout) == {
            "decision": "block",
            "reason": "check_inしてください",
        }

    def test_approve_reason付き(self):
        harness, stdout = _make()
        harness.emit_approve("上限到達")
        assert _emitted(stdout) == {"decision": "approve", "reason": "上限到達"}

    def test_approve_reason省略時はdecisionのみ(self):
        harness, stdout = _make()
        harness.emit_approve()
        assert _emitted(stdout) == {"decision": "approve"}

    def test_empty(self):
        harness, stdout = _make()
        harness.emit_empty()
        assert _emitted(stdout) == {}

    def test_updated_tool_outputは出力してTrueを返す(self):
        harness, stdout = _make(hook_event_name="PostToolUse")
        updated = {"content": [{"type": "text", "text": "sanitized"}]}
        assert harness.emit_updated_tool_output(updated) is True
        assert _emitted(stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated,
            }
        }

    def test_display_contentは出力してTrueを返す(self):
        harness, stdout = _make(hook_event_name="MessageDisplay")
        assert harness.emit_display_content("M#1 (タイトル)") is True
        assert _emitted(stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "MessageDisplay",
                "displayContent": "M#1 (タイトル)",
            }
        }

    def test_monitor_watchをサポートする(self):
        harness, _ = _make()
        assert harness.supports_monitor_watch is True


# ---------------------------------------------------------------------------
# 2. transcript読み書き
# ---------------------------------------------------------------------------


def _write_jsonl(path, entries):
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


class TestReadTranscriptEntries:
    def test_フラット形式を中間表現へ正規化する(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        tool_use_block = {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "mcp__plugin_calm_calm__check_in",
            "input": {"activity_id": 7},
        }
        _write_jsonl(
            transcript,
            [
                {"type": "user", "message": {"content": "こんにちは"}},
                {"type": "assistant", "message": {"content": [tool_use_block]}},
                {"type": "human", "message": {"content": "旧形式"}},
                {"type": "user", "isMeta": True, "message": {"content": "注入"}},
                {"type": "summary", "summary": "..."},
            ],
        )
        harness = ClaudeCodeHarness()

        entries = harness.read_transcript_entries(str(transcript))

        assert [e.kind for e in entries] == [
            "user",
            "assistant",
            "user",
            "user",
            "other",
        ]
        # 文字列contentはtext block 1個に正規化される
        assert entries[0].content == [{"type": "text", "text": "こんにちは"}]
        # list contentのblockはそのまま保持される（tool_use入力へ到達できる）
        assert entries[1].content == [tool_use_block]
        assert entries[2].content == [{"type": "text", "text": "旧形式"}]
        assert [e.is_meta for e in entries] == [False, False, False, True, False]
        # rawは元エントリ無加工
        assert entries[0].raw == {"type": "user", "message": {"content": "こんにちは"}}

    def test_壊れた行と非dict行は読み飛ばす(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "a"}}\n'
            "{broken\n"
            "[1, 2]\n"
            '{"type": "user", "message": {"content": "b"}}\n',
            encoding="utf-8",
        )
        harness = ClaudeCodeHarness()

        entries = harness.read_transcript_entries(str(transcript))

        assert [e.content[0]["text"] for e in entries] == ["a", "b"]

    def test_ファイル不在は空リスト(self, tmp_path):
        harness = ClaudeCodeHarness()
        assert harness.read_transcript_entries(str(tmp_path / "none.jsonl")) == []


class TestReadTranscriptEntriesFromOffset:
    def test_差分読みは新規エントリだけを返す(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [{"type": "user", "message": {"content": "1件目"}}])
        harness = ClaudeCodeHarness()

        first, offset, reset = harness.read_transcript_entries_from_offset(
            str(transcript), 0
        )
        assert [e.content[0]["text"] for e in first] == ["1件目"]
        assert reset is False

        with open(transcript, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"type": "user", "message": {"content": "2件目"}},
                    ensure_ascii=False,
                )
                + "\n"
            )

        second, new_offset, reset = harness.read_transcript_entries_from_offset(
            str(transcript), offset
        )
        assert [e.content[0]["text"] for e in second] == ["2件目"]
        assert reset is False
        assert new_offset == transcript.stat().st_size

    def test_オフセットがファイルサイズ超過なら全読みしてリセットを報告する(
        self, tmp_path
    ):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [{"type": "user", "message": {"content": "a"}}])
        harness = ClaudeCodeHarness()

        entries, offset, reset = harness.read_transcript_entries_from_offset(
            str(transcript), 10_000
        )

        assert reset is True
        assert [e.content[0]["text"] for e in entries] == ["a"]
        assert offset == transcript.stat().st_size


class TestRewriteTranscriptEntry:
    def test_uuid一致行だけを書き換え他行は保持する(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        entries = [
            {"type": "user", "uuid": "u-1", "message": {"content": "前"}},
            {"type": "user", "uuid": "u-2", "message": {"content": "対象"}},
            {"type": "user", "uuid": "u-3", "message": {"content": "後"}},
        ]
        _write_jsonl(transcript, entries)
        harness = ClaudeCodeHarness()
        target = harness.read_transcript_entries(str(transcript))[1]
        target.raw["message"]["content"] = "書換済"

        assert harness.rewrite_transcript_entry(str(transcript), target) is True

        lines = transcript.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == entries[0]
        assert json.loads(lines[1]) == {
            "type": "user",
            "uuid": "u-2",
            "message": {"content": "書換済"},
        }
        assert json.loads(lines[2]) == entries[2]
        # JSONL構造の維持（末尾改行・行数）
        assert transcript.read_text(encoding="utf-8").endswith("\n")
        assert len(lines) == 3

    def test_uuidが無いエントリはFalseでファイルを変更しない(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [{"type": "user", "message": {"content": "a"}}])
        before = transcript.read_text(encoding="utf-8")
        harness = ClaudeCodeHarness()
        entry = TranscriptEntry(
            kind="user", content=[], raw={"type": "user", "message": {"content": "x"}}
        )

        assert harness.rewrite_transcript_entry(str(transcript), entry) is False
        assert transcript.read_text(encoding="utf-8") == before

    def test_一致するuuidが無ければFalseでファイルを変更しない(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(
            transcript, [{"type": "user", "uuid": "u-1", "message": {"content": "a"}}]
        )
        before = transcript.read_text(encoding="utf-8")
        harness = ClaudeCodeHarness()
        entry = TranscriptEntry(kind="user", content=[], raw={"uuid": "missing"})

        assert harness.rewrite_transcript_entry(str(transcript), entry) is False
        assert transcript.read_text(encoding="utf-8") == before
