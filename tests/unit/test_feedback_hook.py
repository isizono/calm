"""hooks/feedback_hook.py のテスト。

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

from hooks import feedback_hook
from src.services.feedback_rules import is_human_speaker

# hook_contextが読む環境変数。実行機の値でテストが揺れないよう毎回除去する。
_HOOK_CONTEXT_ENV_VARS = (
    "TERM",
    "TERM_PROGRAM",
    "TMUX",
    "STY",
    "SSH_TTY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
)


@pytest.fixture(autouse=True)
def _feedback_log_path(tmp_path, monkeypatch):
    """hookのログ出力先を毎回tmp_pathへ向け、開発機の実ログファイルを汚染しない。"""
    monkeypatch.setenv("CALM_FEEDBACK_LOG_PATH", str(tmp_path / "feedback_hook.jsonl"))


@pytest.fixture(autouse=True)
def _clean_hook_context_env(monkeypatch):
    """hook_contextが読む環境変数を毎回除去し、テストを実行機の環境から独立させる。"""
    for name in _HOOK_CONTEXT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def feedback_db(temp_db, monkeypatch):
    """全migration適用済みの一時DB（temp_db）をfeedback_hookのDB解決先にも向ける。"""
    monkeypatch.setenv("CALM_DB_PATH", temp_db)
    return temp_db


def _run_hook(monkeypatch, capsys, payload: dict):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    feedback_hook.main()
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
    def test_writes_boundary_row(self, feedback_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"},
        )
        rows = _rows(feedback_db, "boundary")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"

    def test_missing_session_id_writes_nothing(self, feedback_db, monkeypatch, capsys):
        _run_hook(monkeypatch, capsys, {"hook_event_name": "SessionStart"})
        assert _rows(feedback_db, "boundary") == []


class TestUserPromptSubmit:
    def test_writes_utterance_with_strong_flag(self, feedback_db, monkeypatch, capsys):
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
        rows = _rows(feedback_db, "utterance")
        assert len(rows) == 1
        assert rows[0]["text"] == "前にも言ったよね、それ"
        assert rows[0]["flag"] == "strong"
        assert rows[0]["prompt_id"] == "p1"

    def test_no_vocab_hit_leaves_flag_null(self, feedback_db, monkeypatch, capsys):
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
        rows = _rows(feedback_db, "utterance")
        assert rows[0]["flag"] is None

    def test_writes_speaker_when_single_match_found(self, feedback_db, monkeypatch, capsys, tmp_path):
        # isMetaキー欠落・isSidechain=false・origin={"kind": ...} は
        # preflight_origins.md実測のtyped|human組(104件)の実際の形。isMetaを
        # 省略するのは、isMeta/isSidechainを取り違えても両方Falseで区別が付かない
        # 事態を避けるため（.get()の既定値はNoneでFalseと異なる）。
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
                "cwd": "/work/dir",
            },
        )
        utterance_rows = _rows(feedback_db, "utterance")
        speaker_rows = _rows(feedback_db, "speaker")
        assert len(speaker_rows) == 1
        assert speaker_rows[0]["ref_id"] == utterance_rows[0]["id"]
        speaker = json.loads(speaker_rows[0]["text"])
        assert speaker == {
            "promptSource": "typed",
            "turnOrigin": "human",
            "entrypoint": "cli",
            "userType": "external",
            "isMeta": None,
            "isSidechain": False,
            "origin": {"kind": "x"},
            "hook_context": {
                "cwd": "/work/dir",
                "transcript_path": transcript_path,
                "agent_type": None,
                "term": None,
                "term_program": None,
                "tmux": False,
                "sty": False,
                "ssh_tty": False,
                "stdin_isatty": False,
                "stdout_isatty": False,
                "claudecode": None,
                "claude_code_entrypoint": None,
                "claude_code_child_session": None,
            },
        }

    def test_no_speaker_when_two_rows_match(self, feedback_db, monkeypatch, capsys, tmp_path):
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
        assert _rows(feedback_db, "speaker") == []

    def test_no_speaker_when_no_row_matches(self, feedback_db, monkeypatch, capsys, tmp_path):
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
        assert _rows(feedback_db, "speaker") == []


class TestHookContext:
    """話者の行に足した、transcriptの7キーの外の生値（判定には使わない）。"""

    def _human_transcript(self, tmp_path, prompt_id="p1", text="hello there"):
        return _write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "promptId": prompt_id,
                    "message": {"content": text},
                    "promptSource": "typed",
                    "turnOrigin": "human",
                }
            ],
        )

    def test_records_hook_input_and_env_values_when_present(
        self, feedback_db, monkeypatch, capsys, tmp_path
    ):
        """agent_type（サブエージェントの判別材料）と端末の手がかりが実際に来たら記録する。"""
        transcript_path = self._human_transcript(tmp_path)
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123,0")
        monkeypatch.setenv("CLAUDECODE", "1")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "hello there",
                "transcript_path": transcript_path,
                "agent_type": "scout",
            },
        )
        hook_context = json.loads(_rows(feedback_db, "speaker")[0]["text"])["hook_context"]
        assert hook_context["agent_type"] == "scout"
        assert hook_context["term"] == "xterm-256color"
        assert hook_context["tmux"] is True
        assert hook_context["sty"] is False
        assert hook_context["claudecode"] == "1"

    def test_varying_hook_context_does_not_change_human_classification(
        self, feedback_db, monkeypatch, capsys, tmp_path
    ):
        """足した値を変えても、人間かどうかの判定(turnOrigin/promptSourceだけ)は変わらない。"""
        transcript_a = self._human_transcript(tmp_path, prompt_id="p1", text="first")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "first",
                "transcript_path": transcript_a,
                "cwd": "/work/dir/a",
                "agent_type": "scout",
            },
        )
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123,0")
        transcript_b = self._human_transcript(tmp_path, prompt_id="p2", text="second")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s2",
                "prompt_id": "p2",
                "prompt": "second",
                "transcript_path": transcript_b,
                "cwd": "/other/dir/b",
                "agent_type": None,
            },
        )
        speaker_a, speaker_b = (json.loads(r["text"]) for r in _rows(feedback_db, "speaker"))

        # 足した値(hook_context)は実際に異なる
        assert speaker_a["hook_context"]["cwd"] != speaker_b["hook_context"]["cwd"]
        assert speaker_a["hook_context"]["tmux"] != speaker_b["hook_context"]["tmux"]

        # 判定に使う2キーは両方とも同じで、判定結果も変わらない
        assert speaker_a["turnOrigin"] == speaker_b["turnOrigin"] == "human"
        assert speaker_a["promptSource"] == speaker_b["promptSource"] == "typed"
        assert (
            is_human_speaker(speaker_a["turnOrigin"], speaker_a["promptSource"])
            == is_human_speaker(speaker_b["turnOrigin"], speaker_b["promptSource"])
            is True
        )

    def test_missing_values_stay_null_not_dropped(self, feedback_db, monkeypatch, capsys, tmp_path):
        """hook入力にも環境にも無い値は、キーごと落とさずnullのまま残す。"""
        transcript_path = self._human_transcript(tmp_path)
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
        hook_context = json.loads(_rows(feedback_db, "speaker")[0]["text"])["hook_context"]
        for key in ("cwd", "agent_type", "term", "term_program", "claudecode",
                    "claude_code_entrypoint", "claude_code_child_session"):
            assert key in hook_context
            assert hook_context[key] is None


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

    def test_writes_tool_row(self, feedback_db, monkeypatch, capsys):
        self._post_tool_use(monkeypatch, capsys)
        rows = _rows(feedback_db, "tool")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "Bash"
        assert "ls" in rows[0]["text"]

    def test_missing_tool_name_writes_nothing(self, feedback_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_input": {}},
        )
        assert _rows(feedback_db, "tool") == []

    def test_40_cap_and_single_overflow_row(self, feedback_db, monkeypatch, capsys):
        for i in range(42):
            self._post_tool_use(monkeypatch, capsys, tool_use_id=f"t{i}")
        tool_rows = _rows(feedback_db, "tool")
        overflow_rows = _rows(feedback_db, "tool_overflow")
        assert len(tool_rows) == 40
        assert len(overflow_rows) == 1

    def test_cap_groups_by_null_prompt_id(self, feedback_db, monkeypatch, capsys):
        """prompt_idが無い呼び出し（サブエージェント経路）でも上限が正しく効く。"""
        for i in range(41):
            self._post_tool_use(monkeypatch, capsys, prompt_id=None, tool_use_id=f"t{i}")
        assert len(_rows(feedback_db, "tool")) == 40
        assert len(_rows(feedback_db, "tool_overflow")) == 1

    def test_tool_input_summary_truncated_to_300_chars(self, feedback_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": "Bash",
                "tool_input": {"command": "x" * 500},
                "tool_use_id": "t1",
            },
        )
        rows = _rows(feedback_db, "tool")
        assert len(rows[0]["text"]) == 300


class TestPostToolUseFailure:
    def test_writes_tool_fail_row_truncated(self, feedback_db, monkeypatch, capsys):
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
        rows = _rows(feedback_db, "tool_fail")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "Bash"
        assert len(rows[0]["text"]) == 1000

    def test_missing_tool_name_writes_nothing(self, feedback_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "PostToolUseFailure", "session_id": "s1", "error": "boom"},
        )
        assert _rows(feedback_db, "tool_fail") == []


class TestStop:
    def test_writes_reply_and_advances_cursor(self, feedback_db, monkeypatch, capsys, tmp_path):
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
        rows = _rows(feedback_db, "reply")
        assert len(rows) == 1
        assert rows[0]["text"] == "hello back"
        assert rows[0]["prompt_id"] == "p1"

        conn = sqlite3.connect(feedback_db)
        try:
            cursor_row = conn.execute(
                "SELECT byte_offset FROM feedback_cursor WHERE session_id = 's1'"
            ).fetchone()
        finally:
            conn.close()
        expected_offset = os.path.getsize(transcript_path)
        assert cursor_row[0] == expected_offset

    def test_does_not_reread_already_consumed_bytes(self, feedback_db, monkeypatch, capsys, tmp_path):
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
        rows = _rows(feedback_db, "reply")
        assert len(rows) == 1
        assert rows[0]["text"] == "second turn reply"

    def test_backfills_speaker_for_pending_utterance(self, feedback_db, monkeypatch, capsys, tmp_path):
        conn = sqlite3.connect(feedback_db)
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
        speaker_rows = _rows(feedback_db, "speaker")
        assert len(speaker_rows) == 1
        assert json.loads(speaker_rows[0]["text"])["promptSource"] == "typed"

    def test_uses_last_assistant_message_when_transcript_has_no_reply(
        self, feedback_db, monkeypatch, capsys, tmp_path
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
        rows = _rows(feedback_db, "reply")
        assert len(rows) == 1
        assert rows[0]["text"] == "fallback reply text"
        assert rows[0]["prompt_id"] == "p1"


class TestModeSwitch:
    def test_off_mode_writes_nothing(self, feedback_db, monkeypatch, capsys):
        conn = sqlite3.connect(feedback_db)
        try:
            conn.execute("UPDATE feedback_meta SET mode = 'off' WHERE id = 1")
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
        assert _rows(feedback_db, "utterance") == []

    def test_observe_mode_still_writes(self, feedback_db, monkeypatch, capsys):
        # temp_db の既定は 'observe'。off にしていなければ書き込まれることの対照。
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "SessionStart", "session_id": "s1"},
        )
        assert len(_rows(feedback_db, "boundary")) == 1


class TestFailOpen:
    def test_missing_table_does_not_raise(self, monkeypatch, capsys, tmp_path):
        """フィードバック機構のテーブルが無ければ何もしない（マイグレーション未適用のDB）。"""
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
        feedback_hook.main()
        assert capsys.readouterr().out == ""

    def test_unknown_hook_event_name_is_ignored(self, feedback_db, monkeypatch, capsys):
        _run_hook(monkeypatch, capsys, {"hook_event_name": "SubagentStop", "session_id": "s1"})
        # どのkindも書かれない
        for kind in ("boundary", "utterance", "reply", "tool", "tool_fail"):
            assert _rows(feedback_db, kind) == []

    def test_handler_exception_rolls_back_earlier_writes_in_same_transaction(
        self, feedback_db, monkeypatch, capsys, tmp_path
    ):
        """1回の起動の書き込みは1トランザクション: 途中で例外が起きたら直前までの書き込みも差し戻す。"""
        transcript_path = _write_transcript(
            tmp_path,
            [
                {
                    "type": "assistant",
                    "promptId": "p1",
                    "uuid": "a-1",
                    "message": {"content": [{"type": "text", "text": "first reply"}]},
                },
                {
                    "type": "assistant",
                    "promptId": ["not-a-string"],  # sqlite3がbindできず例外になる
                    "uuid": "a-2",
                    "message": {"content": [{"type": "text", "text": "second reply"}]},
                },
            ],
        )
        _run_hook(
            monkeypatch, capsys,
            {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": transcript_path},
        )
        assert _rows(feedback_db, "reply") == []

        conn = sqlite3.connect(feedback_db)
        try:
            cursor_row = conn.execute(
                "SELECT byte_offset FROM feedback_cursor WHERE session_id = 's1'"
            ).fetchone()
        finally:
            conn.close()
        assert cursor_row is None  # カーソル更新も同じトランザクションに含まれ差し戻る
