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
from src.services import vessel_service
from src.services.vessel_rules import (
    APPEND_LESSON_TOOL,
    GET_LESSONS_TOOL,
    RECORD_LESSON_TOOL,
    is_human_speaker,
)

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
def _vessel_log_path(tmp_path, monkeypatch):
    """hookのログ出力先を毎回tmp_pathへ向け、開発機の実ログファイルを汚染しない。"""
    monkeypatch.setenv("CALM_VESSEL_LOG_PATH", str(tmp_path / "vessel_hook.jsonl"))


@pytest.fixture(autouse=True)
def _clean_hook_context_env(monkeypatch):
    """hook_contextが読む環境変数を毎回除去し、テストを実行機の環境から独立させる。"""
    for name in _HOOK_CONTEXT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vessel_db(temp_db, monkeypatch):
    """全migration適用済みの一時DB（temp_db）をvessel_hookのDB解決先にも向ける。"""
    monkeypatch.setenv("CALM_DB_PATH", temp_db)
    return temp_db


def _run_hook(monkeypatch, capsys, payload: dict, *, allow_output: bool = False):
    """hookを実行する。allow_output=Falseの既定では標準出力が空であることも検証する

    (停止スイッチが観測だけの状態、またはoffのときの契約)。mode='on'での判定結果
    出力を検証するテストだけallow_output=Trueを渡す。
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    vessel_hook.main()
    captured = capsys.readouterr()
    if not allow_output:
        assert captured.out == "", "hookは標準出力に何も書かない(mode='on'の判定結果を除く)"
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


def _set_mode(vessel_db, mode: str) -> None:
    conn = sqlite3.connect(vessel_db)
    try:
        conn.execute("UPDATE vessel_meta SET mode = ? WHERE id = 1", (mode,))
        conn.commit()
    finally:
        conn.close()


def _tool_response(result: dict) -> dict:
    """MCPツールの結果をtool_response(dict + contentブロック)の形に包む。"""
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _human_turn(monkeypatch, capsys, tmp_path, vessel_db, session_id: str, prompt_id: str, text: str) -> int:
    """UserPromptSubmitを実際に発火させ、人間として確定した発話行のidを返す。

    実測(preflight_origins.md)のtyped|human組と同じ形の値を使う。
    """
    transcript_path = _write_transcript(
        tmp_path,
        [
            {
                "type": "user",
                "promptId": prompt_id,
                "message": {"content": text},
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
            "session_id": session_id,
            "prompt_id": prompt_id,
            "prompt": text,
            "transcript_path": transcript_path,
        },
    )
    return [r for r in _rows(vessel_db, "utterance") if r["prompt_id"] == prompt_id][-1]["id"]


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
        self, vessel_db, monkeypatch, capsys, tmp_path
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
        hook_context = json.loads(_rows(vessel_db, "speaker")[0]["text"])["hook_context"]
        assert hook_context["agent_type"] == "scout"
        assert hook_context["term"] == "xterm-256color"
        assert hook_context["tmux"] is True
        assert hook_context["sty"] is False
        assert hook_context["claudecode"] == "1"

    def test_varying_hook_context_does_not_change_human_classification(
        self, vessel_db, monkeypatch, capsys, tmp_path
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
        speaker_a, speaker_b = (json.loads(r["text"]) for r in _rows(vessel_db, "speaker"))

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

    def test_missing_values_stay_null_not_dropped(self, vessel_db, monkeypatch, capsys, tmp_path):
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
        hook_context = json.loads(_rows(vessel_db, "speaker")[0]["text"])["hook_context"]
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

    def test_tool_input_summary_truncated_to_300_chars(self, vessel_db, monkeypatch, capsys):
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
        rows = _rows(vessel_db, "tool")
        assert len(rows[0]["text"]) == 300


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

    def test_handler_exception_rolls_back_earlier_writes_in_same_transaction(
        self, vessel_db, monkeypatch, capsys, tmp_path
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
        assert _rows(vessel_db, "reply") == []

        conn = sqlite3.connect(vessel_db)
        try:
            cursor_row = conn.execute(
                "SELECT byte_offset FROM vessel_cursor WHERE session_id = 's1'"
            ).fetchone()
        finally:
            conn.close()
        assert cursor_row is None  # カーソル更新も同じトランザクションに含まれ差し戻る


class TestBind:
    """PostToolUseの書き込みの結び付け(bind)。"""

    def test_writes_bind_only_from_write_tool_success(self, vessel_db, monkeypatch, capsys):
        create = vessel_service.record_lesson(kind="tally", handle="h-bind-ok", body="握り方を統一する")
        assert create["ok"] is True

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        bind_rows = _rows(vessel_db, "bind")
        assert len(bind_rows) == 1
        assert bind_rows[0]["lesson_id"] == create["lesson_id"]
        assert bind_rows[0]["entry_id"] is None

    def test_other_tools_write_no_bind(self, vessel_db, monkeypatch, capsys):
        create = vessel_service.record_lesson(kind="tally", handle="h-not-vessel", body="関係ない知見")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        assert _rows(vessel_db, "bind") == []

    def test_rejected_write_tool_call_writes_no_bind(self, vessel_db, monkeypatch, capsys):
        vessel_service.record_lesson(kind="tally", handle="h-dup", body="先に作った知見")
        rejected = vessel_service.record_lesson(kind="tally", handle="h-dup", body="重複するhandle")
        assert rejected["ok"] is False

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(rejected),
                "tool_use_id": "t1",
            },
        )
        assert _rows(vessel_db, "bind") == []

    def test_tool_name_mismatch_is_logged(self, vessel_db, monkeypatch, capsys, tmp_path):
        log_path = tmp_path / "vessel_hook.jsonl"
        monkeypatch.setenv("CALM_VESSEL_LOG_PATH", str(log_path))
        create = vessel_service.record_lesson(kind="tally", handle="h-renamed", body="別名で配信された想定")

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                # プラグイン名・サーバー名を変えて入れ直した場合の想定(末尾は一致するが完全一致しない)
                "tool_name": "mcp__claude_ai_calm__record_lesson",
                "tool_input": {},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        assert _rows(vessel_db, "bind") == []
        lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        mismatch_lines = [line for line in lines if line.get("why") == "tool_name_mismatch"]
        assert len(mismatch_lines) == 1
        assert mismatch_lines[0]["tool_name"] == "mcp__claude_ai_calm__record_lesson"


class TestQuoteSearch:
    """PostToolUseのbindが行う引用探索。"""

    def test_hits_prior_turn_when_current_turn_has_no_match(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        older_id = _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p0", "むかしの話")
        prior_id = _human_turn(
            monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "pkillは危ないから使わないで"
        )
        _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p2", "さっきの件、直しておいて")

        create = vessel_service.record_lesson(
            kind="tally", handle="h-quote-prior", body="pkillの使用を戒める", quote="pkillは危ない"
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p2",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "pkillは危ない"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        bind_rows = _rows(vessel_db, "bind")
        assert bind_rows[0]["ref_id"] == prior_id
        assert bind_rows[0]["ref_id"] != older_id

    def test_prefers_current_turn_over_prior_turn_on_ambiguous_match(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "これは間違いです")
        current_id = _human_turn(
            monkeypatch, capsys, tmp_path, vessel_db, "s1", "p2", "これは間違いです、直しておいて"
        )

        create = vessel_service.record_lesson(
            kind="tally", handle="h-quote-current", body="間違いの言い回し", quote="これは間違いです"
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p2",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "これは間違いです"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        bind_rows = _rows(vessel_db, "bind")
        assert bind_rows[0]["ref_id"] == current_id

    def test_prefers_current_turn_even_when_a_later_turn_has_a_higher_id(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        """PostToolUseの処理が次のターン開始後にずれ込んでも、今のターンの発話を優先する。"""
        current_id = _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "これは間違いです")
        # p1のPostToolUseがまだ処理されないうちに次のターンp2が始まり、同じ文言の
        # 発話が記録された(idはp1より大きい)状態を模す。
        _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p2", "これは間違いです")

        create = vessel_service.record_lesson(
            kind="tally", handle="h-quote-turn-race", body="間違いの言い回し", quote="これは間違いです"
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "これは間違いです"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        bind_rows = _rows(vessel_db, "bind")
        assert bind_rows[0]["ref_id"] == current_id

    def test_ignores_subagent_utterance(self, vessel_db, monkeypatch, capsys):
        # サブエージェント発のutterance行は実運用では生じないが(UserPromptSubmitは
        # 対話下・印字モードいずれのサブエージェントでも発火しない)、
        # find_quote_refのagent_id IS NULLフィルタ自体はDBレベルで直接検証する。
        conn = sqlite3.connect(vessel_db)
        try:
            conn.execute(
                "INSERT INTO obs_events (session_id, prompt_id, agent_id, kind, text) "
                "VALUES ('s1', 'p1', 'sub-1', 'utterance', 'pkillは危ないから使わないで')"
            )
            conn.commit()
        finally:
            conn.close()

        create = vessel_service.record_lesson(
            kind="tally", handle="h-quote-subagent", body="pkillの使用を戒める", quote="pkillは危ない"
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "pkillは危ない"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        assert _rows(vessel_db, "bind")[0]["ref_id"] is None

    def test_quote_not_found_leaves_ref_empty_and_view_falls_to_ai(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "こんにちは")
        create = vessel_service.record_lesson(
            kind="tally", handle="h-quote-missing", body="どこにも無い引用",
            quote="どこにも存在しない文字列です",
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "どこにも存在しない文字列です"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        bind_rows = _rows(vessel_db, "bind")
        assert bind_rows[0]["ref_id"] is None

        conn = sqlite3.connect(vessel_db)
        try:
            origin = conn.execute(
                "SELECT origin FROM lesson_origin WHERE lesson_id = ?", (create["lesson_id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert origin == "ai"


class TestBindProtectedFeedback:
    """並列書き込みで守られた知見になった後のbindの『効かない』理由。"""

    def test_body_entry_bound_after_lesson_becomes_protected_reports_ineffective(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        _set_mode(vessel_db, "on")
        _human_turn(
            monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "以前と同じ間違いです、直しておいて"
        )

        create = vessel_service.record_lesson(
            kind="tally", handle="h-protected", body="いずれ守られる知見", quote="以前と同じ間違いです",
        )
        # 並列呼び出しの想定: 作成のbindがまだ無い時点でbody追記が先に成立する
        body_append = vessel_service.append_lesson(handle="h-protected", kind="body", body="直した本文")
        assert body_append["ok"] is True

        # 作成のbindを先に処理する(人間由来になり守られた知見になる)
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "以前と同じ間違いです"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t-create",
            },
            allow_output=True,
        )

        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": APPEND_LESSON_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(body_append),
                "tool_use_id": "t-body",
            },
            allow_output=True,
        )
        assert "h-protected" in captured.out
        assert "効かない" in captured.out


class TestBindOriginFeedback:
    """mode='on'のときのbind判定結果の出し分け。"""

    def test_human_origin_via_matched_quote_in_same_turn(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        _set_mode(vessel_db, "on")
        _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "同じ間違いを繰り返さないで")

        create = vessel_service.record_lesson(
            kind="tally", handle="h-human-origin", body="繰り返しを戒める知見", quote="同じ間違いを繰り返さないで",
        )
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "同じ間違いを繰り返さないで"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
            allow_output=True,
        )
        assert "出自=人間" in captured.out
        assert "h-human-origin" in captured.out

    def test_ai_origin_when_no_utterance_in_turn(self, vessel_db, monkeypatch, capsys):
        _set_mode(vessel_db, "on")
        create = vessel_service.record_lesson(kind="tally", handle="h-ai-origin", body="発話の無いターンの知見")
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
            allow_output=True,
        )
        assert "出自=AI（quote指定なし）" in captured.out

    def test_ai_origin_reports_quote_not_found(self, vessel_db, monkeypatch, capsys, tmp_path):
        _set_mode(vessel_db, "on")
        _human_turn(monkeypatch, capsys, tmp_path, vessel_db, "s1", "p1", "こんにちは")
        create = vessel_service.record_lesson(
            kind="tally", handle="h-ai-quote-missing", body="見つからない引用の知見",
            quote="どこにも存在しない文字列です",
        )
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "どこにも存在しない文字列です"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
            allow_output=True,
        )
        assert "出自=AI（引用が見つからない）" in captured.out

    def test_ai_origin_reports_quote_from_non_human_utterance(
        self, vessel_db, monkeypatch, capsys, tmp_path
    ):
        """promptSource='system'・turnOrigin='task_notification'は実測で観測された非人間の組。"""
        _set_mode(vessel_db, "on")
        transcript_path = _write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "promptId": "p1",
                    "message": {"content": "定期実行の通知メッセージ"},
                    "promptSource": "system",
                    "turnOrigin": "task_notification",
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
                "prompt": "定期実行の通知メッセージ",
                "transcript_path": transcript_path,
            },
        )

        create = vessel_service.record_lesson(
            kind="tally", handle="h-ai-nonhuman-quote", body="人間でない発話からの引用の知見",
            quote="定期実行の通知メッセージ",
        )
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {"quote": "定期実行の通知メッセージ"},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
            allow_output=True,
        )
        assert "出自=AI（引用の発話が人間でない）" in captured.out

    def test_pending_when_turn_utterance_has_no_speaker_yet(self, vessel_db, monkeypatch, capsys):
        _set_mode(vessel_db, "on")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "まだtranscriptに現れていない発話",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        create = vessel_service.record_lesson(kind="tally", handle="h-pending", body="判定待ちの知見")
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
            allow_output=True,
        )
        assert "出自はターン末に決まる" in captured.out

    def test_observe_mode_writes_bind_but_returns_no_feedback(self, vessel_db, monkeypatch, capsys):
        # temp_dbの既定(observe)のまま: 書き込みは起きるが標準出力は空
        create = vessel_service.record_lesson(kind="tally", handle="h-observe", body="観測だけの状態の知見")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": RECORD_LESSON_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(create),
                "tool_use_id": "t1",
            },
        )
        assert len(_rows(vessel_db, "bind")) == 1


class TestPull:
    """get_lessonsの成功結果からの`delivered`(pull)。"""

    def test_writes_delivered_for_each_delivered_handle(self, vessel_db, monkeypatch, capsys):
        vessel_service.record_lesson(
            kind="prevent", handle="h-pull-1", body="pullで届く知見1",
            deliver_event="prompt", deliver_spec={"all": [{"field": "prompt", "op": "len_gt", "value": 0}]},
            step_event="tool_call", step_spec={"tool": "Bash", "all": [{"field": "command", "op": "len_gt", "value": 0}]},
        )
        result = vessel_service.get_lessons(handle="h-pull-1")
        assert result["delivered_handles"], "配達する種類なので delivered_handles に載るはず"

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": GET_LESSONS_TOOL,
                "tool_input": {"handle": "h-pull-1"},
                "tool_response": _tool_response(result),
                "tool_use_id": "t1",
            },
        )
        pull_rows = [r for r in _rows(vessel_db, "delivered") if r["channel"] == "pull"]
        assert len(pull_rows) == 1

    def test_no_delivered_handles_writes_nothing(self, vessel_db, monkeypatch, capsys):
        result = {"ok": True, "items": [], "delivered_handles": []}
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "prompt_id": "p1",
                "tool_name": GET_LESSONS_TOOL,
                "tool_input": {},
                "tool_response": _tool_response(result),
                "tool_use_id": "t1",
            },
        )
        assert _rows(vessel_db, "delivered") == []


class TestHumanWithdrawDeclaration:
    """UserPromptSubmitの人間の1行の撤回宣言。"""

    def test_standalone_line_writes_human_withdraw(self, vessel_db, monkeypatch, capsys):
        vessel_service.record_lesson(kind="tally", handle="withdraw-me", body="撤回対象の知見")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "知見撤回 withdraw-me",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        rows = _rows(vessel_db, "human_withdraw")
        assert len(rows) == 1

    def test_multiple_lines_write_one_row_each(self, vessel_db, monkeypatch, capsys):
        # tally種は条件を持てず(kind_mismatch)、record_lessonの重複検査は現在の
        # 条件の完全一致で見るため、同じDBに条件無しのtallyを2件は作れない。
        # 2件目はprevent種で別条件にする。
        vessel_service.record_lesson(kind="tally", handle="withdraw-a", body="pgrepの多用を戒める")
        vessel_service.record_lesson(
            kind="prevent", handle="withdraw-b", body="worktreeを使わない直接checkoutを戒める",
            deliver_event="tool_call",
            deliver_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": "withdraw-b"}]},
            step_event="tool_call",
            step_spec={"tool": "Bash", "all": [{"field": "command", "op": "regex", "value": "withdraw-b"}]},
        )
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "知見撤回 withdraw-a\n知見撤回 withdraw-b",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        assert len(_rows(vessel_db, "human_withdraw")) == 2

    @pytest.mark.parametrize(
        "prompt",
        [
            "知見撤回 withdraw-me はしないで",       # 否定文（行の途中まで一致しても全体は一致しない)
            "知見撤回 withdraw-me？",                # 疑問文
            "これは知見撤回 withdraw-me です",       # 行の途中
            "> 知見撤回 withdraw-me",                # 引用行
            "```\n知見撤回 withdraw-me\n```",        # コードブロックの中
        ],
    )
    def test_non_standalone_forms_do_not_write(self, vessel_db, monkeypatch, capsys, prompt):
        vessel_service.record_lesson(kind="tally", handle="withdraw-me", body="撤回対象の知見")
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": prompt,
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        assert _rows(vessel_db, "human_withdraw") == []

    def test_unknown_handle_writes_nothing(self, vessel_db, monkeypatch, capsys):
        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "知見撤回 no-such-handle",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        assert _rows(vessel_db, "human_withdraw") == []

    def test_already_withdrawn_handle_writes_nothing_more(self, vessel_db, monkeypatch, capsys):
        vessel_service.record_lesson(kind="tally", handle="withdraw-twice", body="既に撤回済みの知見")
        withdrawn = vessel_service.append_lesson(handle="withdraw-twice", kind="withdraw")
        assert withdrawn["ok"] is True

        _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "知見撤回 withdraw-twice",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        assert _rows(vessel_db, "human_withdraw") == []


class TestHumanWithdrawFeedback:
    """mode='on'のときの撤回結果の1行。"""

    def test_on_mode_returns_withdraw_result_line(self, vessel_db, monkeypatch, capsys):
        _set_mode(vessel_db, "on")
        vessel_service.record_lesson(kind="tally", handle="withdraw-feedback", body="撤回対象の知見")
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "知見撤回 withdraw-feedback",
                "transcript_path": "/nonexistent.jsonl",
            },
            allow_output=True,
        )
        assert "撤回になった" in captured.out
        assert "withdraw-feedback" in captured.out

    def test_observe_mode_writes_row_but_returns_nothing(self, vessel_db, monkeypatch, capsys):
        # temp_dbの既定(observe)のまま
        vessel_service.record_lesson(kind="tally", handle="withdraw-observe", body="撤回対象の知見")
        captured = _run_hook(
            monkeypatch, capsys,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt_id": "p1",
                "prompt": "知見撤回 withdraw-observe",
                "transcript_path": "/nonexistent.jsonl",
            },
        )
        assert captured.out == ""
        assert len(_rows(vessel_db, "human_withdraw")) == 1
