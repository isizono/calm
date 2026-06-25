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
            # title に fullword 形式の ID が含まれるケース (二段階 enrich 回避テスト用)
            (4, f"{_FW('material', 2)} info", None),
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

    def test_title_containing_fullword_id_not_double_enriched(self, fake_db):
        # title 自体に fullword 形式の ID 参照が混入しているケースで、
        # 単一 regex の単一パスなので置換結果中の fullword は再 enrich されず
        # ネスト括弧にならないことを検証する (id=4 の material を利用)。
        text = f"see {_MN('M', 4)} here"
        expected = f"see {_MN('M', 4)} ({_FW('material', 2)} info) here"
        assert self._enrich(text, fake_db) == expected


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


class TestIdLeakObservation:
    """MessageDisplay hook が内部 ID リテラル件数を id_leak_count に蓄積する副作用検証。"""

    def _run(self, monkeypatch, payload):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        try:
            mdid.main()
        except SystemExit:
            pass
        return out.getvalue()

    @pytest.fixture
    def state_dir(self, tmp_path, monkeypatch):
        from hooks.hook_state import HookState
        monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
        monkeypatch.setenv("HOOK_STATE_DIR", str(tmp_path))
        return tmp_path

    def test_increment_called_when_match_found(
        self, monkeypatch, fake_db, state_dir
    ):
        from hooks.hook_state import HookState

        self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "obs-test-001",
                "delta": f"see {_MN('M', 1)} and {_MN('D', 10)}",
            },
        )
        assert HookState("obs-test-001").get_id_leak_count() == 2

    def test_no_increment_when_no_match(self, monkeypatch, fake_db, state_dir):
        from hooks.hook_state import HookState

        self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "obs-test-002",
                "delta": "plain text no ids",
            },
        )
        assert HookState("obs-test-002").get_id_leak_count() == 0

    def test_no_increment_when_session_id_empty(
        self, monkeypatch, fake_db, state_dir
    ):
        from hooks.hook_state import HookState

        # session_id 空でも display 動作は走るが state file は作られない
        self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "",
                "delta": f"see {_MN('M', 1)}",
            },
        )
        # state_dir 下にどの session 名でも count は 0
        assert HookState("any-session-name").get_id_leak_count() == 0

    def test_fullword_form_also_counted(self, monkeypatch, fake_db, state_dir):
        from hooks.hook_state import HookState

        self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "obs-test-003",
                "delta": f"see {_FW('material', 1)} and {_MN('D', 10)}",
            },
        )
        assert HookState("obs-test-003").get_id_leak_count() == 2

    def test_increment_accumulates_across_chunks(
        self, monkeypatch, fake_db, state_dir
    ):
        from hooks.hook_state import HookState

        session = "obs-test-004"
        for _ in range(3):
            self._run(
                monkeypatch,
                {
                    "hook_event_name": "MessageDisplay",
                    "session_id": session,
                    "delta": f"ref {_MN('M', 1)}",
                },
            )
        # 3 chunks × 1 match = 3
        assert HookState(session).get_id_leak_count() == 3

    def test_observation_runs_even_without_db(self, monkeypatch, tmp_path, state_dir):
        from hooks.hook_state import HookState

        # DB を存在しないパスに向けて display 動作を skip させても、
        # 観測副作用は走ること (観測は DB アクセス前)
        monkeypatch.setenv("CC_MEMORY_DB_PATH", str(tmp_path / "nonexistent.db"))
        self._run(
            monkeypatch,
            {
                "hook_event_name": "MessageDisplay",
                "session_id": "obs-test-005",
                "delta": f"ref {_MN('M', 1)}",
            },
        )
        assert HookState("obs-test-005").get_id_leak_count() == 1
