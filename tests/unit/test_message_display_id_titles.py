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


@pytest.fixture(autouse=True)
def session_registry_path(tmp_path, monkeypatch):
    """対応表ファイルを実マシンの ``~/.cc-memory/session_aliases.json`` から
    隔離する。既定ではファイルが存在しないパスを指す (=対応表なし状態)。
    セッション名の対応表が要るテストは、返された path に
    `_write_registry` で書き込んで使う。
    """
    path = tmp_path / "session_aliases.json"
    monkeypatch.setenv("CALM_SESSION_REGISTRY_PATH", str(path))
    return path


def _write_registry(path: Path, sessions: dict) -> None:
    """`register_checkin` が書き込む形 (``{"version": 1, "sessions": {...}}``)
    で対応表ファイルを書く。
    """
    path.write_text(
        json.dumps({"version": 1, "sessions": sessions}, ensure_ascii=False),
        encoding="utf-8",
    )


def _registry_entry(
    name: str,
    *,
    alias: str,
    alias_source: str = "derived",
    activity_title: str = "",
    activity_status: str = "in_progress",
    alias_activity_id: int = 1,
) -> dict:
    """`register_checkin` が書く1セッション分の row と同じ形を組み立てる。"""
    return {
        "name": name,
        "alias": alias,
        "alias_source": alias_source,
        "alias_activity_id": alias_activity_id,
        "activity_title": activity_title,
        "activity_status": activity_status,
        "cli_pid": 99999,
        "cwd": "/tmp/example",
        "bridge_session_id": "bridge-test",
        "updated_at": "2026-09-27T00:00:00Z",
    }


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
            (2, "a" * (mdid.TITLE_MAX + 5), None),
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
    monkeypatch.setenv("CALM_DB_PATH", str(db_path))
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


class TestSessionNameEnrich:
    def _enrich(self, text, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return mdid._enrich(text, conn)
        finally:
            conn.close()

    def test_registered_name_replaced(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="alias unused", activity_title="[作業] 何とかを直す")},
        )
        out = self._enrich("いま workspace-1b で作業中", fake_db)
        assert out == "いま <Session: 何とかを直す> で作業中"

    def test_unregistered_name_untouched(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] foo")},
        )
        out = self._enrich("workspace-9z は対応表に無い", fake_db)
        assert out == "workspace-9z は対応表に無い"

    def test_long_name_not_matched_as_substring(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] foo")},
        )
        text = "workspace-1bc は別セッション名"
        assert self._enrich(text, fake_db) == text

    def test_hyphen_boundary_not_matched(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] foo")},
        )
        text = "workspace-1b-x も別セッション名"
        assert self._enrich(text, fake_db) == text

    def test_longest_name_wins_when_prefix_of_another(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {
                "s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] short"),
                "s2": _registry_entry("workspace-1b-x", alias="b", activity_title="[作業] long"),
            },
        )
        out = self._enrich("workspace-1b-x が対象", fake_db)
        assert out == "<Session: long> が対象"

    def test_derived_uses_activity_title_not_alias(self, fake_db, session_registry_path):
        # alias と activity_title を意図的に違う値にし、alias が誤って
        # 使われていないことを保証する (derived branch)。
        _write_registry(
            session_registry_path,
            {
                "s1": _registry_entry(
                    "workspace-1b",
                    alias="[作業] foo-2",
                    alias_source="derived",
                    activity_title="[作業] foo",
                )
            },
        )
        out = self._enrich("workspace-1b にて", fake_db)
        assert out == "<Session: foo> にて"

    def test_manual_alias_source_uses_alias(self, fake_db, session_registry_path):
        # alias と activity_title を意図的に違う値にし、activity_title が
        # 誤って使われていないことを保証する (manual branch)。
        _write_registry(
            session_registry_path,
            {
                "s1": _registry_entry(
                    "workspace-1b",
                    alias="手動名",
                    alias_source="manual",
                    activity_title="[作業] 別のタイトル",
                )
            },
        )
        out = self._enrich("workspace-1b にて", fake_db)
        assert out == "<Session: 手動名> にて"

    def test_bracket_tag_stripped_and_truncated(self, fake_db, session_registry_path):
        long_title = "[作業] " + "あ" * (mdid.TITLE_MAX + 5)
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title=long_title)},
        )
        out = self._enrich("workspace-1b にて", fake_db)
        assert out == f"<Session: {'あ' * mdid.TITLE_MAX}…> にて"

    def test_empty_display_not_replaced(self, fake_db, session_registry_path):
        # 角カッコの札だけで実体が無いタイトルは表示名が空になり、置換しない。
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業]")},
        )
        text = "workspace-1b にて"
        assert self._enrich(text, fake_db) == text

    def test_inline_code_not_replaced(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] foo")},
        )
        text = "コード内 `workspace-1b` はそのまま、地の文の workspace-1b は置換"
        out = self._enrich(text, fake_db)
        assert out == "コード内 `workspace-1b` はそのまま、地の文の <Session: foo> は置換"

    def test_inserted_display_not_double_replaced(self, fake_db, session_registry_path):
        # 挿入した表示名の中にセッション名リテラルが含まれていても、
        # 単一 regex の単一パスなので再置換されない (title 自体に
        # 'workspace-1b' を含む material を使う)。
        conn = sqlite3.connect(fake_db)
        conn.execute(
            "INSERT INTO materials (id, title, retracted_at) VALUES (?, ?, ?)",
            (5, "workspace-1b について", None),
        )
        conn.commit()
        conn.close()
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] foo")},
        )
        text = f"see {_MN('M', 5)} here"
        out = self._enrich(text, fake_db)
        assert out == f"see {_MN('M', 5)} (workspace-1b について) here"
        assert "<Session:" not in out

    def test_inserted_session_display_not_double_replaced(self, fake_db, session_registry_path):
        # 挿入した表示名 (activity_title 由来) 自体に内部IDリテラルと別の
        # セッション名が混入していても、単一 regex の単一パスなので
        # 再置換されない。
        _write_registry(
            session_registry_path,
            {
                "s1": _registry_entry(
                    "workspace-1b",
                    alias="a",
                    activity_title=f"[作業] {_MN('M', 1)} と workspace-2c",
                ),
                "s2": _registry_entry("workspace-2c", alias="b", activity_title="[作業] bar"),
            },
        )
        out = self._enrich("workspace-1b にて", fake_db)
        assert out == f"<Session: {_MN('M', 1)} と workspace-2c> にて"
        assert out.count("<Session:") == 1
        assert "(short title)" not in out

    def test_id_and_session_name_mixed_in_one_chunk(self, fake_db, session_registry_path):
        _write_registry(
            session_registry_path,
            {"s1": _registry_entry("workspace-1b", alias="a", activity_title="[作業] foo")},
        )
        out = self._enrich(f"{_MN('M', 1)} は workspace-1b が担当", fake_db)
        assert out == f"{_MN('M', 1)} (short title) は <Session: foo> が担当"

    def test_missing_registry_file_id_enrichment_still_works(self, fake_db):
        # session_registry_path fixture は既定で「ファイル無し」状態にする。
        text = f"{_MN('M', 1)} と workspace-1b"
        out = self._enrich(text, fake_db)
        assert out == f"{_MN('M', 1)} (short title) と workspace-1b"

    def test_broken_registry_json_id_enrichment_still_works(self, fake_db, session_registry_path):
        session_registry_path.write_text("{not valid json", encoding="utf-8")
        text = f"{_MN('M', 1)} と workspace-1b"
        out = self._enrich(text, fake_db)
        assert out == f"{_MN('M', 1)} (short title) と workspace-1b"


class TestMain:
    def _run(self, monkeypatch, payload):
        # CALM_HARNESS未設定 = Claude Code経路に固定する
        monkeypatch.delenv("CALM_HARNESS", raising=False)
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
        monkeypatch.setenv("CALM_DB_PATH", str(tmp_path / "nonexistent.db"))
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

    def test_codex_harness_no_output(self, monkeypatch, fake_db):
        """CALM_HARNESS=codexでは表示書き換え機構が無い（emit_display_content
        がFalse）ため、enrich対象のIDがあっても何も出力しない。"""
        monkeypatch.setenv("CALM_HARNESS", "codex")
        payload = {
            "hook_event_name": "MessageDisplay",
            "delta": f"ref {_MN('M', 1)} now",
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        try:
            mdid.main()
        except SystemExit:
            pass
        assert out.getvalue() == ""
