"""hooks/vessel_hook.py のテスト。

各イベントの観測の書き込み、ツール呼び出し40件の上限と超過の行、停止スイッチが
offのときに何も書かないこと、テーブルが無いときに落ちないこと、例外が出ても
差し戻さないこと、標準出力が常に空であることを検証する。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile

import pytest

from hooks import vessel_hook


@pytest.fixture
def vessel_db(temp_db, monkeypatch):
    """全migration適用済みの一時DB（temp_db）をvessel_hookのDB解決先にも向ける。"""
    monkeypatch.setenv("CALM_DB_PATH", temp_db)
    return temp_db


def _run_hook(monkeypatch, capsys, payload: dict):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    vessel_hook.main()
    captured = capsys.readouterr()
    assert captured.out == "", "hookは標準出力に何も書かない"
    return captured


def _rows(db_path: str, kind: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM obs_events WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    finally:
        conn.close()


def _write_transcript(tmp_path, lines: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return str(path)


class TestSessionStart:
    def test_writes_boundary_row(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"},
        )
        rows = _rows(vessel_db, "boundary")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"

    def test_missing_session_id_writes_nothing(self, vessel_db, monkeypatch, capsys):
        _run_hook(monkeypatch, capsys, {"hook_event_name": "SessionStart"})
        assert _rows(vessel_db, "boundary") == []


class TestUserPromptSubmit:
    def test_writes_utterance_with_strong_flag(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "前にも言ったよね、それ",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        rows = _rows(vessel_db, "utterance")
        assert len(rows) == 1
        assert rows[0]["text"] == "前にも言ったよね、それ"
        assert rows[0]["flag"] == "strong"
        assert rows[0]["prompt_id"] == "p1"

    def test_no_vocab_hit_leaves_flag_null(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "ありがとう",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        rows = _rows(vessel_db, "utterance")
        assert rows[0]["flag"] is None

    def test_writes_speaker_when_single_match_found(self, vessel_db, monkeypatch, capsys, tmp_path):
        transcript_path = _write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "promptId": "p1",
                    "message": {"content": "hello there"},
                    "promptSource": "typed",
                    "turnOrigin": "human",
                    "entrypoint": "cli",
                    "userType": "external",
                    "isMeta": False,
                    "isSidechain": False,
                    "origin": {"kind": "x"},
                }
            ],
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "hello there",
                "transcript_path": transcript_path,
            },
        )
        utterance_rows = _rows(vessel_db, "utterance")
        speaker_rows = _rows(vessel_db, "speaker")
        assert len(speaker_rows) == 1
        assert speaker_rows[0]["ref_id"] == utterance_rows[0]["id"]
        speaker = json.loads(speaker_rows[0]["text"])
        assert speaker == {
            "promptSource": "typed",
            "turnOrigin": "human",
            "entrypoint": "cli",
            "userType": "external",
            "isMeta": False,
            "isSidechain": False,
            "origin": {"kind": "x"},
        }

    def test_no_speaker_when_two_rows_match(self, vessel_db, monkeypatch, capsys, tmp_path):
        """本文が同じ行が2行あれば人間でない扱い（speakerを書かない）。"""
        duplicate_row = {
            "type": "user",
            "promptId": "p1",
            "message": {"content": "hello there"},
            "promptSource": "typed",
            "turnOrigin": "human",
        }
        transcript_path = _write_transcript(tmp_path, [duplicate_row, duplicate_row])
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "hello there",
                "transcript_path": transcript_path,
            },
        )
        assert _rows(vessel_db, "speaker") == []

    def test_no_speaker_when_no_row_matches(self, vessel_db, monkeypatch, capsys, tmp_path):
        transcript_path = _write_transcript(
            tmp_path,
            [{"type": "user", "promptId": "p1", "message": {"content": "different text"}}],
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "hello there",
                "transcript_path": transcript_path,
            },
        )
        assert _rows(vessel_db, "speaker") == []


class TestPostToolUse:
    def _post_tool_use(self, monkeypatch, capsys, session_id="s1", prompt_id="p1", tool_use_id="t1"):
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "prompt_id": prompt_id,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": tool_use_id,
            },
        )

    def test_writes_tool_row(self, vessel_db, monkeypatch, capsys):
        self._post_tool_use(monkeypatch, capsys)
        rows = _rows(vessel_db, "tool")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "Bash"
        assert "ls" in rows[0]["text"]

    def test_missing_tool_name_writes_nothing(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_input": {}},
        )
        assert _rows(vessel_db, "tool") == []

    def test_40_cap_and_single_overflow_row(self, vessel_db, monkeypatch, capsys):
        for i in range(42):
            self._post_tool_use(monkeypatch, capsys, tool_use_id=f"t{i}")
        tool_rows = _rows(vessel_db, "tool")
        overflow_rows = _rows(vessel_db, "tool_overflow")
        assert len(tool_rows) == 40
        assert len(overflow_rows) == 1

    def test_cap_groups_by_null_prompt_id(self, vessel_db, monkeypatch, capsys):
        """prompt_idが無い呼び出し（サブエージェント経路）でも上限が正しく効く。"""
        for i in range(41):
            self._post_tool_use(monkeypatch, capsys, prompt_id=None, tool_use_id=f"t{i}")
        assert len(_rows(vessel_db, "tool")) == 40
        assert len(_rows(vessel_db, "tool_overflow")) == 1


class TestPostToolUseFailure:
    def test_writes_tool_fail_row_truncated(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": "Bash",
                "tool_input": {"command": "false"},
                "error": "x" * 2000,
                "tool_use_id": "t1",
            },
        )
        rows = _rows(vessel_db, "tool_fail")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "Bash"
        assert len(rows[0]["text"]) == 1000

    def test_missing_tool_name_writes_nothing(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "PostToolUseFailure", "session_id": "s1", "error": "boom"},
        )
        assert _rows(vessel_db, "tool_fail") == []


class TestStop:
    def test_writes_reply_and_advances_cursor(self, vessel_db, monkeypatch, capsys, tmp_path):
        transcript_path = _write_transcript(
            tmp_path,
            [
                {"type": "user", "promptId": "p1", "message": {"content": "hi"}},
                {
                    "type": "assistant",
                    "promptId": "p1",
                    "uuid": "a-1",
                    "message": {"content": [{"type": "text", "text": "hello back"}]},
                },
            ],
        )
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": transcript_path},
        )
        rows = _rows(vessel_db, "reply")
        assert len(rows) == 1
        assert rows[0]["text"] == "hello back"
        assert rows[0]["prompt_id"] == "p1"

        conn = sqlite3.connect(vessel_db)
        try:
            cursor_row = conn.execute(
                "SELECT byte_offset FROM vessel_cursor WHERE session_id = 's1'"
            ).fetchone()
        finally:
            conn.close()
        expected_offset = os.path.getsize(transcript_path)
        assert cursor_row[0] == expected_offset

    def test_does_not_reread_already_consumed_bytes(self, vessel_db, monkeypatch, capsys, tmp_path):
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            json.dumps({"type": "user", "promptId": "p1", "message": {"content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(path)},
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "promptId": "p1",
                        "uuid": "a-1",
                        "message": {"content": [{"type": "text", "text": "second turn reply"}]},
                    }
                )
                + "\n"
            )
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(path)},
        )
        rows = _rows(vessel_db, "reply")
        assert len(rows) == 1
        assert rows[0]["text"] == "second turn reply"

    def test_backfills_speaker_for_pending_utterance(self, vessel_db, monkeypatch, capsys, tmp_path):
        conn = sqlite3.connect(vessel_db)
        try:
            conn.execute(
                "INSERT INTO obs_events (session_id, prompt_id, kind, text) "
                "VALUES ('s1', 'p1', 'utterance', 'delayed prompt text')"
            )
            conn.commit()
        finally:
            conn.close()

        transcript_path = _write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "promptId": "p1",
                    "message": {"content": "delayed prompt text"},
                    "promptSource": "typed",
                    "turnOrigin": "human",
                }
            ],
        )
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": transcript_path},
        )
        speaker_rows = _rows(vessel_db, "speaker")
        assert len(speaker_rows) == 1
        assert json.loads(speaker_rows[0]["text"])["promptSource"] == "typed"

    def test_uses_last_assistant_message_when_transcript_has_no_reply(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        transcript_path = _write_transcript(
            tmp_path, [{"type": "user", "promptId": "p1", "message": {"content": "hi"}}]
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "Stop",
                "session_id": "s1",
                "transcript_path": transcript_path,
                "last_assistant_message": "fallback reply text",
            },
        )
        rows = _rows(vessel_db, "reply")
        assert len(rows) == 1
        assert rows[0]["text"] == "fallback reply text"
        assert rows[0]["prompt_id"] == "p1"


class TestModeSwitch:
    def test_off_mode_writes_nothing(self, vessel_db, monkeypatch, capsys):
        conn = sqlite3.connect(vessel_db)
        try:
            conn.execute("UPDATE vessel_meta SET mode = 'off' WHERE id = 1")
            conn.commit()
        finally:
            conn.close()

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "前にも言ったよね",
            },
        )
        assert _rows(vessel_db, "utterance") == []

    def test_observe_mode_still_writes(self, vessel_db, monkeypatch, capsys):
        # temp_db の既定は 'observe'。off にしていなければ書き込まれることの対照。
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "SessionStart", "session_id": "s1"},
        )
        assert len(_rows(vessel_db, "boundary")) == 1


class TestFailOpen:
    def test_missing_table_does_not_raise(self, monkeypatch, capsys, tmp_path):
        """器のテーブルが無ければ何もしない（マイグレーション未適用のDB）。"""
        empty_db = tmp_path / "empty.db"
        sqlite3.connect(str(empty_db)).close()
        monkeypatch.setenv("CALM_DB_PATH", str(empty_db))

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "hello",
            },
        )  # 例外を出さず、標準出力も空であることをアサート済み

    def test_unreadable_db_path_does_not_raise(self, monkeypatch, capsys, tmp_path):
        """DBを開けない（ディレクトリを指す等）ときもfail-openで落ちない。"""
        monkeypatch.setenv("CALM_DB_PATH", str(tmp_path))  # ディレクトリをDBパスとして渡す

        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "SessionStart", "session_id": "s1"},
        )

    def test_malformed_json_input_does_not_raise(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
        vessel_hook.main()
        assert capsys.readouterr().out == ""

    def test_unknown_hook_event_name_is_ignored(self, vessel_db, monkeypatch, capsys):
        _run_hook(monkeypatch, capsys, {"hook_event_name": "SubagentStop", "session_id": "s1"})
        # どのkindも書かれない
        for kind in ("boundary", "utterance", "reply", "tool", "tool_fail"):
            assert _rows(vessel_db, kind) == []
