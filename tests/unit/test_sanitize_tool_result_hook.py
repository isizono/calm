"""hooks/sanitize_tool_result_hook.py のユニットテスト。

plan-c.md エッジケース表 #1-#13 を網羅する。
"""
import io
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks import sanitize_tool_result_hook
from src.services.citations_pure import TYPE_TO_TABLE

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CITATION_EVENT_LOG_DDL = """
CREATE TABLE citation_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL CHECK (source IN (
        'write_auto_convert', 'bulk_migration',
        'transcript_post_tool_use', 'transcript_session_start_backfill',
        'external_doc_sanitize'
    )),
    tool_name TEXT,
    target_entity_type TEXT CHECK (target_entity_type IS NULL OR target_entity_type IN (
        'decision', 'activity', 'log', 'material', 'topic'
    )),
    target_entity_id INTEGER,
    target_field TEXT,
    before_text TEXT NOT NULL,
    after_text TEXT NOT NULL,
    verified_at TEXT,
    verification_result TEXT CHECK (verification_result IS NULL OR verification_result IN (
        'exists', 'dangling', 'skip'
    )),
    extra_json TEXT
);
"""


@pytest.fixture
def fixture_db(monkeypatch):
    """citation_event_log + 最小 entity テーブル + M#1/D#1/L#1/A#1/T#1 を持つ一時 DB。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = sqlite3.connect(db_path)
        try:
            for table in TYPE_TO_TABLE.values():
                conn.execute(
                    f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"
                )
                conn.execute(f"INSERT INTO {table} (id) VALUES (1)")
            conn.executescript(_CITATION_EVENT_LOG_DDL)
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("CC_MEMORY_DB_PATH", db_path)
        monkeypatch.delenv("CC_MEMORY_SANITIZE_DISABLE", raising=False)
        yield db_path


def _run_hook(stdin_payload: dict) -> tuple[str, int]:
    """stdin を差し替えて main() を実行し (stdout, exit_code) を返す。"""
    fake_stdin = io.StringIO(json.dumps(stdin_payload))
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    with patch.object(sanitize_tool_result_hook.sys, "stdin", fake_stdin), \
         patch.object(sanitize_tool_result_hook.sys, "stdout", fake_stdout), \
         patch.object(sanitize_tool_result_hook.sys, "stderr", fake_stderr):
        code = sanitize_tool_result_hook.main()
    return fake_stdout.getvalue(), code


def _read_citation_events(db_path: str) -> list[dict]:
    """citation_event_log の全行を読み、extra_json を dict に展開して返す。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT source, tool_name, before_text, after_text, verification_result, "
            "extra_json FROM citation_event_log ORDER BY id"
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["extra"] = json.loads(d.pop("extra_json"))
            results.append(d)
        return results
    finally:
        conn.close()


_TOOL_NAME = "mcp__plugin_claude-code-memory_cc-memory__check_in"


def _payload(content, *, tool_name=_TOOL_NAME, cwd="/tmp/outside-repo",
             session_id="sess-abc", extra_response=None):
    response = {"content": content}
    if extra_response:
        response.update(extra_response)
    return {
        "tool_name": tool_name,
        "tool_response": response,
        "cwd": cwd,
        "session_id": session_id,
        "transcript_path": "/tmp/transcripts/test.jsonl",
    }


# ---------------------------------------------------------------------------
# Case #1: 有効 target の生 X#NNN → {{cite:X#NNN}} に変換
# ---------------------------------------------------------------------------


def test_case_01_valid_target_converted_to_cite(fixture_db):
    stdout, code = _run_hook(_payload("ref to M#1 and D#1 here"))
    assert code == 0
    out = json.loads(stdout)
    hook_out = out["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    sanitized = hook_out["updatedToolOutput"]["content"]
    assert sanitized == [
        {"type": "text", "text": "ref to {{cite:M#1}} and {{cite:D#1}} here"}
    ]

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["source"] == "transcript_post_tool_use"
    assert events[0]["tool_name"] == _TOOL_NAME
    assert events[0]["before_text"] == "ref to M#1 and D#1 here"
    assert events[0]["after_text"] == "ref to {{cite:M#1}} and {{cite:D#1}} here"
    assert events[0]["verification_result"] == "exists"
    assert events[0]["extra"]["session_id"] == "sess-abc"
    assert events[0]["extra"]["sanitized_count"] == 2
    assert events[0]["extra"]["deleted_count"] == 0


# ---------------------------------------------------------------------------
# Case #2: cc-memory tool 以外 → no-op (matcher で限定だが防御)
# ---------------------------------------------------------------------------


def test_case_02_non_cc_memory_tool_is_noop(fixture_db):
    payload = _payload("ref to M#1", tool_name="Read")
    stdout, code = _run_hook(payload)
    assert code == 0
    assert stdout == ""  # no updatedToolOutput 出力
    assert _read_citation_events(fixture_db) == []


# ---------------------------------------------------------------------------
# Case #3: CC_MEMORY_SANITIZE_DISABLE=1 → 即 exit 0、log なし
# ---------------------------------------------------------------------------


def test_case_03_env_disable_short_circuits(fixture_db, monkeypatch):
    monkeypatch.setenv("CC_MEMORY_SANITIZE_DISABLE", "1")
    stdout, code = _run_hook(_payload("ref to M#1"))
    assert code == 0
    assert stdout == ""
    assert _read_citation_events(fixture_db) == []


# ---------------------------------------------------------------------------
# Case #4: cwd が cc-memory リポジトリ内 → skip (pyproject.toml で判定)
# ---------------------------------------------------------------------------


def test_case_04_cwd_in_cc_memory_repo_skipped(fixture_db, tmp_path):
    repo_root = tmp_path / "cc-memory-repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "claude-code-memory"\nversion = "0.1.0"\n'
    )
    subdir = repo_root / "src" / "deep" / "nested"
    subdir.mkdir(parents=True)

    payload = _payload("ref to M#1", cwd=str(subdir))
    stdout, code = _run_hook(payload)
    assert code == 0
    assert stdout == ""
    assert _read_citation_events(fixture_db) == []


def test_case_04_cwd_in_unrelated_project_not_skipped(fixture_db, tmp_path):
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / "pyproject.toml").write_text(
        '[project]\nname = "some-other-package"\nversion = "0.1.0"\n'
    )
    payload = _payload("ref to M#1", cwd=str(other_root))
    stdout, code = _run_hook(payload)
    assert code == 0
    out = json.loads(stdout)
    assert out["hookSpecificOutput"]["updatedToolOutput"]["content"] == [
        {"type": "text", "text": "ref to {{cite:M#1}}"}
    ]


# ---------------------------------------------------------------------------
# Case #5: content に X#NNN が無い → 何も変換しない、本文が変化しないので event も記録しない
# ---------------------------------------------------------------------------


def test_case_05_no_raw_literals_logs_nothing(fixture_db):
    stdout, code = _run_hook(_payload("plain text without any ids"))
    assert code == 0
    out = json.loads(stdout)
    assert out["hookSpecificOutput"]["updatedToolOutput"]["content"] == [
        {"type": "text", "text": "plain text without any ids"}
    ]

    assert _read_citation_events(fixture_db) == []


# ---------------------------------------------------------------------------
# Case #6: コードブロック内の X#NNN → 変換されない (生のまま保持)
# ---------------------------------------------------------------------------


def test_case_06_code_block_literals_preserved(fixture_db):
    payload = _payload("see `M#1 inline` and outside D#1 too")
    stdout, code = _run_hook(payload)
    assert code == 0
    out = json.loads(stdout)
    sanitized = out["hookSpecificOutput"]["updatedToolOutput"]["content"]
    assert sanitized == [
        {"type": "text", "text": "see `M#1 inline` and outside {{cite:D#1}} too"}
    ]

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["verification_result"] == "exists"
    assert events[0]["extra"]["sanitized_count"] == 1
    assert events[0]["extra"]["deleted_count"] == 0
    # skipped_in_codeblock はコードブロック内でスキップした件数 (M#1 inline の1件)
    assert events[0]["extra"]["skipped_in_codeblock"] == 1


def test_case_06_fenced_code_block_preserved(fixture_db):
    payload = _payload("intro\n```\nM#1 inside fence\n```\nafter D#1")
    stdout, code = _run_hook(payload)
    assert code == 0
    out = json.loads(stdout)
    sanitized = out["hookSpecificOutput"]["updatedToolOutput"]["content"]
    assert isinstance(sanitized, list)
    sanitized_text = sanitized[0]["text"]
    assert "M#1 inside fence" in sanitized_text
    assert "{{cite:D#1}}" in sanitized_text
    assert sanitized_text.count("{{cite:") == 1


# ---------------------------------------------------------------------------
# Case #7: dangling (target 不在) → [deleted X#NNN]、failed_count にカウント
# ---------------------------------------------------------------------------


def test_case_07_dangling_target_becomes_deleted_marker(fixture_db):
    payload = _payload("known M#1, missing M#9999999 here")
    stdout, code = _run_hook(payload)
    assert code == 0
    out = json.loads(stdout)
    sanitized = out["hookSpecificOutput"]["updatedToolOutput"]["content"]
    assert sanitized == [
        {"type": "text", "text": "known {{cite:M#1}}, missing [deleted M#9999999] here"}
    ]

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    # dangling が1件以上含まれる場合、verification_result は 'dangling' (混在時の代表値)
    assert events[0]["verification_result"] == "dangling"
    assert events[0]["after_text"] == "known {{cite:M#1}}, missing [deleted M#9999999] here"
    assert events[0]["extra"]["sanitized_count"] == 1
    assert events[0]["extra"]["deleted_count"] == 1


# ---------------------------------------------------------------------------
# Case #8: sqlite3.OperationalError (DB lock 等) → warning + failure_reason 記録
# ---------------------------------------------------------------------------


def test_case_08_sqlite_operational_error_logged_as_failure(fixture_db, monkeypatch):
    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sanitize_tool_result_hook, "_sanitize_content", boom)

    stdout, code = _run_hook(_payload("M#1 something"))
    assert code == 0
    # 変換失敗時は updatedToolOutput を返さない (transcript を壊さない)
    assert stdout == ""
    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["before_text"] == ""
    assert events[0]["after_text"] == ""
    assert events[0]["verification_result"] is None
    assert events[0]["extra"]["error"] == "database is locked"


# ---------------------------------------------------------------------------
# Case #9: Python 例外 (JSON parse 失敗) → warning + failure イベント記録
# ---------------------------------------------------------------------------


def test_case_09_invalid_json_logs_failure_event(fixture_db):
    fake_stdin = io.StringIO("not-a-json-blob")
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    with patch.object(sanitize_tool_result_hook.sys, "stdin", fake_stdin), \
         patch.object(sanitize_tool_result_hook.sys, "stdout", fake_stdout), \
         patch.object(sanitize_tool_result_hook.sys, "stderr", fake_stderr):
        code = sanitize_tool_result_hook.main()
    assert code == 0
    assert fake_stdout.getvalue() == ""
    assert "[sanitize_tool_result_hook]" in fake_stderr.getvalue()
    # citation_event_log には session_id/transcript_path 専用カラムが無いため
    # (CHECK 制約も無い)、不明なままでも failure イベントが1件記録される
    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["verification_result"] is None
    assert events[0]["extra"]["session_id"] is None
    assert "error" in events[0]["extra"]


# ---------------------------------------------------------------------------
# Case #10: updatedToolOutput が公式 schema で出力される
# ---------------------------------------------------------------------------


def test_case_10_official_hook_output_schema(fixture_db):
    stdout, code = _run_hook(_payload("ref to M#1"))
    assert code == 0
    out = json.loads(stdout)
    assert set(out.keys()) == {"hookSpecificOutput"}
    hook_out = out["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    assert "updatedToolOutput" in hook_out
    assert isinstance(hook_out["updatedToolOutput"], dict)
    assert "content" in hook_out["updatedToolOutput"]
    assert isinstance(hook_out["updatedToolOutput"]["content"], list)


# ---------------------------------------------------------------------------
# Case #11: read-only conn が使われる (DB が ?mode=ro で開ける限り動作)
# ---------------------------------------------------------------------------


def test_case_11_read_only_conn_is_used_for_validation(fixture_db, monkeypatch):
    """read-only モードで read conn が開かれる: ファイル mode に依存せず動作する。

    sqlite3.connect の uri 引数を確認することで read-only 経路を検証する。
    """
    seen_uris: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        if args:
            seen_uris.append((args[0], kwargs.get("uri", False)))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sanitize_tool_result_hook.sqlite3, "connect", spy_connect)

    stdout, code = _run_hook(_payload("ref M#1"))
    assert code == 0
    assert any("mode=ro" in u and uri for u, uri in seen_uris), (
        f"read-only conn (mode=ro) が使われていない: {seen_uris}"
    )


# ---------------------------------------------------------------------------
# Case #12: tool_response の他フィールド (tool_use_id 等) が維持される
# ---------------------------------------------------------------------------


def test_case_12_other_tool_response_fields_preserved(fixture_db):
    payload = _payload(
        "ref M#1",
        extra_response={"tool_use_id": "toolu_01ABC", "is_error": False},
    )
    stdout, code = _run_hook(payload)
    assert code == 0
    out = json.loads(stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["tool_use_id"] == "toolu_01ABC"
    assert updated["is_error"] is False
    assert updated["content"] == [{"type": "text", "text": "ref {{cite:M#1}}"}]


# ---------------------------------------------------------------------------
# Case #14: tool_response が非 dict (生文字列) のとき、updatedToolOutput は
# content block 配列そのものになる (二重ラップされない)
# ---------------------------------------------------------------------------


def test_case_14_non_dict_tool_response_yields_unwrapped_content_block(fixture_db):
    """tool_response が dict でなく生の JSON 文字列そのものの場合、

    updatedToolOutput は content block 配列 ([{"type": "text", "text": ...}]) そのもの
    になり、`{"content": [...]}` のように dict でさらに包まれてはならないことを保証する
    回帰テスト。updatedToolOutput は Claude Code CLI 側でツール結果の content に
    そのままセットされるため、二重にラップすると content が dict になり
    クライアント側の処理がクラッシュする。
    """
    raw_tool_response = json.dumps({"activities": [], "total_count": 0, "archived_tags": []})
    payload = {
        "tool_name": _TOOL_NAME,
        "tool_response": raw_tool_response,
        "cwd": "/tmp/outside-repo",
        "session_id": "sess-raw-string",
        "transcript_path": "/tmp/transcripts/test.jsonl",
    }
    stdout, code = _run_hook(payload)
    assert code == 0
    out = json.loads(stdout)
    updated_output = out["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(updated_output, list)
    assert not isinstance(updated_output, dict)
    assert not isinstance(updated_output, str)
    assert updated_output == [{"type": "text", "text": raw_tool_response}]


# ---------------------------------------------------------------------------
# Case #13: 起動コスト < 1 秒 (PostToolUse 高頻度発火に耐える)
# ---------------------------------------------------------------------------


def test_case_13_hook_completes_under_perf_budget(fixture_db):
    # CI のコンテナスロットリングを考慮した上限 (3.0s)。M#411 §1.2 の hook timeout
    # 600s に対しては十分余裕がある。1s 厳密判定は wall-clock 計測で flake しやすい
    # ため許容幅を持たせる。
    payload = _payload("M#1 D#1 L#1 A#1 T#1 and M#9999 dangling")
    t0 = time.perf_counter()
    _run_hook(payload)
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, f"hook 実行が {elapsed:.3f}s かかった (許容 < 3.0s)"
