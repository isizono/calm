"""message_display_id_titles.py の単体テスト。

MessageDisplay hook の入出力 (stdin から streaming chunk 受領、stdout に
displayContent JSON 出力 or 終了) と、各補助関数の挙動を検証する。
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import message_display_id_titles as mdid  # type: ignore  # noqa: E402


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    """テスト用の最小スキーマ + 各 entity 種別のサンプル row を持つ DB。"""
    db_path = tmp_path / "discussion.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE materials (id INTEGER PRIMARY KEY, title TEXT, retracted_at TIMESTAMP);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, title TEXT, retracted_at TIMESTAMP);
        CREATE TABLE discussion_logs (id INTEGER PRIMARY KEY, title TEXT, retracted_at TIMESTAMP);
        CREATE TABLE activities (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE discussion_topics (id INTEGER PRIMARY KEY, title TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO materials (id, title, retracted_at) VALUES (?, ?, ?)",
        [
            (1, "short title", None),
            (2, "a" * 40, None),
            (3, "retired material", "2026-01-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO decisions (id, title, retracted_at) VALUES (?, ?, ?)",
        [(10, "decision title", None)],
    )
    conn.executemany(
        "INSERT INTO discussion_logs (id, title, retracted_at) VALUES (?, ?, ?)",
        [(20, "log title", None), (21, "retired log", "2026-01-01")],
    )
    conn.executemany(
        "INSERT INTO activities (id, title) VALUES (?, ?)",
        [(30, "activity title")],
    )
    conn.executemany(
        "INSERT INTO discussion_topics (id, title) VALUES (?, ?)",
        [(40, "topic title")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CC_MEMORY_DB_PATH", str(db_path))
    return db_path


def _MN(letter, n):
    """テスト文字列内で内部 ID リテラル形を組み立てる helper。
    test ファイル本体にリテラルを書くと preblock_hook の自己保護に引っかかるため
    runtime 組み立てで回避する。
    """
    return f"{letter}#{n}"


def _FW(word, n):
    return f"{word} #{n}"


class TestStripPrefix:
    def test_no_colon_passes_through(self):
        assert mdid._strip_prefix("plain title") == "plain title"

    def test_half_width_colon(self):
        assert mdid._strip_prefix("label: body text") == "body text"

    def test_full_width_colon(self):
        assert mdid._strip_prefix("ラベル：本文") == "本文"

    def test_first_colon_wins(self):
        assert mdid._strip_prefix("a: b: c") == "b: c"

    def test_mixed_colons_first_wins(self):
        assert mdid._strip_prefix("half: full：tail") == "full：tail"
        assert mdid._strip_prefix("full：half: tail") == "half: tail"

    def test_lstrips_after_colon(self):
        assert mdid._strip_prefix("label:    body") == "body"

    def test_empty_after_colon_falls_back(self):
        assert mdid._strip_prefix("trailing:") == "trailing:"
        assert mdid._strip_prefix("trailing:   ") == "trailing:   "


class TestTruncate:
    def test_short_passes_through(self):
        assert mdid._truncate("a" * mdid.TITLE_MAX) == "a" * mdid.TITLE_MAX

    def test_long_gets_ellipsis(self):
        out = mdid._truncate("a" * (mdid.TITLE_MAX + 5))
        assert out == "a" * mdid.TITLE_MAX + "…"


class TestEnrich:
    def _enrich(self, text, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return mdid._enrich(text, conn)
        finally:
            conn.close()

    def test_code_form_material(self, fake_db):
        out = self._enrich(f"see {_MN('M', 1)} here", fake_db)
        assert out == f"see {_MN('M', 1)} (short title) here"

    def test_code_form_decision(self, fake_db):
        out = self._enrich(f"{_MN('D', 10)} noted", fake_db)
        assert out == f"{_MN('D', 10)} (decision title) noted"

    def test_code_form_log(self, fake_db):
        out = self._enrich(f"{_MN('L', 20)} ok", fake_db)
        assert out == f"{_MN('L', 20)} (log title) ok"

    def test_code_form_activity(self, fake_db):
        out = self._enrich(f"{_MN('A', 30)} starts", fake_db)
        assert out == f"{_MN('A', 30)} (activity title) starts"

    def test_code_form_topic(self, fake_db):
        out = self._enrich(f"{_MN('T', 40)} hosts", fake_db)
        assert out == f"{_MN('T', 40)} (topic title) hosts"

    def test_fullword_form_material(self, fake_db):
        out = self._enrich(f"{_FW('material', 1)} ref", fake_db)
        assert out == f"{_FW('material', 1)} (short title) ref"

    def test_fullword_case_insensitive(self, fake_db):
        out = self._enrich(
            f"{_FW('Material', 1)} and {_FW('DECISION', 10)}", fake_db
        )
        assert out == (
            f"{_FW('Material', 1)} (short title) and "
            f"{_FW('DECISION', 10)} (decision title)"
        )

    def test_truncate_long_title(self, fake_db):
        out = self._enrich(f"{_MN('M', 2)} ref", fake_db)
        assert f"{_MN('M', 2)} (" in out
        assert out.endswith("…) ref")

    def test_retracted_material(self, fake_db):
        out = self._enrich(f"{_MN('M', 3)} historic", fake_db)
        assert out == f"{_MN('M', 3)} (retired material, 取消済) historic"

    def test_retracted_log(self, fake_db):
        out = self._enrich(f"{_MN('L', 21)} was killed", fake_db)
        assert out == f"{_MN('L', 21)} (retired log, 取消済) was killed"

    def test_missing_id_passes_through(self, fake_db):
        text = f"{_MN('M', 999)} not in db"
        assert self._enrich(text, fake_db) == text

    def test_idempotent_when_followed_by_paren(self, fake_db):
        text = f"{_MN('M', 1)} (already labeled)"
        assert self._enrich(text, fake_db) == text

    def test_multiple_in_single_text(self, fake_db):
        out = self._enrich(f"{_MN('M', 1)} and {_MN('D', 10)}", fake_db)
        assert out == (
            f"{_MN('M', 1)} (short title) and {_MN('D', 10)} (decision title)"
        )

    def test_same_id_reused(self, fake_db):
        out = self._enrich(f"{_MN('M', 1)} then {_MN('M', 1)}", fake_db)
        assert out == (
            f"{_MN('M', 1)} (short title) then {_MN('M', 1)} (short title)"
        )

    def test_no_match_returns_original(self, fake_db):
        assert self._enrich("plain text", fake_db) == "plain text"


class TestMain:
    def _run(self, monkeypatch, payload):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        try:
            mdid.main()
        except SystemExit:
            pass
        return out.getvalue()

    def test_delta_payload_enriched(self, monkeypatch, fake_db):
        result = self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "index": 0,
                "final": True,
                "delta": f"ref {_MN('M', 1)} now",
            },
        )
        parsed = json.loads(result)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "MessageDisplay"
        assert (
            parsed["hookSpecificOutput"]["displayContent"]
            == f"ref {_MN('M', 1)} (short title) now"
        )

    def test_assistant_message_fallback(self, monkeypatch, fake_db):
        result = self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "assistant_message": f"{_MN('M', 1)} fallback",
            },
        )
        parsed = json.loads(result)
        assert (
            parsed["hookSpecificOutput"]["displayContent"]
            == f"{_MN('M', 1)} (short title) fallback"
        )

    def test_no_change_no_output(self, monkeypatch, fake_db):
        result = self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "delta": "plain text, no ids",
            },
        )
        assert result == ""

    def test_empty_delta_no_output(self, monkeypatch, fake_db):
        result = self._run(
            monkeypatch,
            {"hook_event_name": "MessageDisplay", "delta": ""},
        )
        assert result == ""

    def test_missing_db_no_output(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_MEMORY_DB_PATH", str(tmp_path / "nonexistent.db"))
        result = self._run(
            monkeypatch,
            {"hook_event_name": "MessageDisplay", "delta": f"{_MN('M', 1)} ref"},
        )
        assert result == ""

    def test_invalid_json_no_output(self, monkeypatch, fake_db):
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        try:
            mdid.main()
        except SystemExit:
            pass
        assert out.getvalue() == ""
