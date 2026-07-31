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


_SANITIZE_LOG_DDL = """
CREATE TABLE sanitize_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    transcript_path TEXT,
    hook_kind TEXT NOT NULL CHECK(hook_kind IN ('post_tool_use', 'session_start_backfill')),
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    sanitized_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(sanitized_count + failed_count <= occurrence_count),
    CHECK(session_id IS NOT NULL OR transcript_path IS NOT NULL)
);
"""


@pytest.fixture
def fixture_db(monkeypatch):
    """sanitize_log + 最小 entity テーブル + M#1/D#1/L#1/A#1/T#1 を持つ一時 DB。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = sqlite3.connect(db_path)
        try:
            for table in TYPE_TO_TABLE.values():
                conn.execute(
                    f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"
                )
                conn.execute(f"INSERT INTO {table} (id) VALUES (1)")
            conn.executescript(_SANITIZE_LOG_DDL)
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


def _read_sanitize_logs(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, transcript_path, hook_kind, occurrence_count, "
            "sanitized_count, failed_count, failure_reason FROM sanitize_log ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
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

    logs = _read_sanitize_logs(fixture_db)
    assert len(logs) == 1
    assert logs[0]["session_id"] == "sess-abc"
    assert logs[0]["hook_kind"] == "post_tool_use"
    assert logs[0]["sanitized_count"] == 2
    assert logs[0]["failed_count"] == 0
    assert logs[0]["occurrence_count"] == 2
    assert logs[0]["failure_reason"] is None


# ---------------------------------------------------------------------------
# Case #2: cc-memory tool 以外 → no-op (matcher で限定だが防御)
# ---------------------------------------------------------------------------


def test_case_02_non_cc_memory_tool_is_noop(fixture_db):
    payload = _payload("ref to M#1", tool_name="Read")
    stdout, code = _run_hook(payload)
    assert code == 0
    assert stdout == ""  # no updatedToolOutput 出力
    assert _read_sanitize_logs(fixture_db) == []


# ---------------------------------------------------------------------------
# Case #3: CC_MEMORY_SANITIZE_DISABLE=1 → 即 exit 0、log なし
# ---------------------------------------------------------------------------


def test_case_03_env_disable_short_circuits(fixture_db, monkeypatch):
    monkeypatch.setenv("CC_MEMORY_SANITIZE_DISABLE", "1")
    stdout, code = _run_hook(_payload("ref to M#1"))
    assert code == 0
    assert stdout == ""
    assert _read_sanitize_logs(fixture_db) == []


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
    assert _read_sanitize_logs(fixture_db) == []


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
# Case #5: content に X#NNN が無い → 何も変換しない、log は count=0 で INSERT
# ---------------------------------------------------------------------------


def test_case_05_no_raw_literals_logs_zero(fixture_db):
    stdout, code = _run_hook(_payload("plain text without any ids"))
    assert code == 0
    out = json.loads(stdout)
    assert out["hookSpecificOutput"]["updatedToolOutput"]["content"] == [
        {"type": "text", "text": "plain text without any ids"}
    ]

    logs = _read_sanitize_logs(fixture_db)
    assert len(logs) == 1
    assert logs[0]["occurrence_count"] == 0
    assert logs[0]["sanitized_count"] == 0
    assert logs[0]["failed_count"] == 0


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

    logs = _read_sanitize_logs(fixture_db)
    assert len(logs) == 1
    assert logs[0]["sanitized_count"] == 1
    assert logs[0]["failed_count"] == 0
    # occurrence は全 X#NNN を含む (コードブロック内も検出件数に含める)
    assert logs[0]["occurrence_count"] == 2


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

    logs = _read_sanitize_logs(fixture_db)
    assert len(logs) == 1
    # dangling は正常変換扱い: failed_count には入らず、occurrence と sanitized の差で
    # 表現される。failed_count は例外失敗のみを表す。
    assert logs[0]["sanitized_count"] == 1
    assert logs[0]["failed_count"] == 0
    assert logs[0]["occurrence_count"] == 2
    assert logs[0]["failure_reason"] is None


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
    logs = _read_sanitize_logs(fixture_db)
    assert len(logs) == 1
    assert logs[0]["failure_reason"] == "database is locked"


# ---------------------------------------------------------------------------
# Case #9: Python 例外 (JSON parse 失敗) → warning + failure_reason 記録
# ---------------------------------------------------------------------------


def test_case_09_invalid_json_logs_failure_reason(fixture_db):
    fake_stdin = io.StringIO("not-a-json-blob")
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    with patch.object(sanitize_tool_result_hook.sys, "stdin", fake_stdin), \
         patch.object(sanitize_tool_result_hook.sys, "stdout", fake_stdout), \
         patch.object(sanitize_tool_result_hook.sys, "stderr", fake_stderr):
        code = sanitize_tool_result_hook.main()
    assert code == 0
    assert fake_stdout.getvalue() == ""
    # session_id / transcript_path が不明なので sanitize_log は記録できない (CHECK 制約)
    # → log 0 件で OK、stderr に警告だけ出る
    assert "[sanitize_tool_result_hook]" in fake_stderr.getvalue()


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
# Case #14: updatedToolOutput.content は常に list 型であり、str 型には絶対にならない
# (content が文字列だとクライアント側で content 配列を前提とした処理がクラッシュする)
# ---------------------------------------------------------------------------


def test_case_14_content_is_always_list_never_string(fixture_db):
    """tool_response が dict でなく生の JSON 文字列そのものの場合でも、

    updatedToolOutput.content は常に content block 配列 ([{"type": "text", "text": ...}])
    になり、str 型には絶対にならないことを保証する回帰テスト。
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
    content = out["hookSpecificOutput"]["updatedToolOutput"]["content"]
    assert isinstance(content, list)
    assert not isinstance(content, str)
    assert content == [{"type": "text", "text": raw_tool_response}]


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
