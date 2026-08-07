"""hooks/sanitize_backfill_hook.py のユニットテスト。

plan-d.md エッジケース表 #1-#15 を網羅する。
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

from hooks import sanitize_backfill_hook
from hooks.hook_state import HookState
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
                conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                conn.execute(f"INSERT INTO {table} (id) VALUES (1)")
            conn.executescript(_CITATION_EVENT_LOG_DDL)
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("CC_MEMORY_DB_PATH", db_path)
        monkeypatch.delenv("CC_MEMORY_SANITIZE_DISABLE", raising=False)
        yield db_path


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """HookState の BASE_DIR を tmp_path に向ける。"""
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


_TOOL_NAME = "mcp__plugin_claude-code-memory_cc-memory__check_in"


def _make_assistant_entry(tool_use_id: str, tool_name: str = _TOOL_NAME) -> dict:
    return {
        "type": "assistant",
        "uuid": f"uuid-asst-{tool_use_id}",
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": {}}
            ]
        },
    }


def _make_user_tool_result_entry(
    tool_use_id: str, content, extra: dict | None = None
) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    entry = {
        "type": "user",
        "uuid": f"uuid-user-{tool_use_id}",
        "timestamp": "2026-06-22T00:00:00Z",
        "message": {"content": [block]},
    }
    if extra:
        entry.update(extra)
    return entry


def _write_transcript(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_transcript_lines(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _run_hook(stdin_payload: dict) -> tuple[str, str, int]:
    """stdin を差し替えて main() を実行し (stdout, stderr, exit_code) を返す。"""
    fake_stdin = io.StringIO(json.dumps(stdin_payload))
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    with patch.object(sanitize_backfill_hook.sys, "stdin", fake_stdin), \
         patch.object(sanitize_backfill_hook.sys, "stdout", fake_stdout), \
         patch.object(sanitize_backfill_hook.sys, "stderr", fake_stderr):
        code = sanitize_backfill_hook.main()
    return fake_stdout.getvalue(), fake_stderr.getvalue(), code


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


def _payload(transcript_path: str, *, session_id="sess-1", cwd="/tmp/outside") -> dict:
    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "source": "resume",
    }


# ---------------------------------------------------------------------------
# Case #1: 初回起動 (sanitize_offset 未設定) → transcript 全体を backfill
# ---------------------------------------------------------------------------


def test_case_01_initial_backfill_whole_transcript(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "found M#1 and D#1 here"),
        _make_assistant_entry("toolu_02"),
        _make_user_tool_result_entry("toolu_02", "another ref M#1"),
    ]
    _write_transcript(transcript, entries)

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    assert "{{cite:M#1}}" in new_entries[1]["message"]["content"][0]["content"]
    assert "{{cite:D#1}}" in new_entries[1]["message"]["content"][0]["content"]
    assert "{{cite:M#1}}" in new_entries[3]["message"]["content"][0]["content"]

    state = HookState("sess-1")
    assert state.get_sanitize_offset() == transcript.stat().st_size

    # 変化した tool_result block 単位で1イベント (block 2件が変化)
    events = _read_citation_events(fixture_db)
    assert len(events) == 2
    assert events[0]["source"] == "transcript_session_start_backfill"
    assert events[0]["tool_name"] == _TOOL_NAME
    assert events[0]["before_text"] == "found M#1 and D#1 here"
    assert events[0]["after_text"] == "found {{cite:M#1}} and {{cite:D#1}} here"
    assert events[0]["verification_result"] == "exists"
    assert events[1]["before_text"] == "another ref M#1"
    assert events[1]["after_text"] == "another ref {{cite:M#1}}"
    total_sanitized = sum(e["extra"]["block_stats"]["sanitized"] for e in events)
    assert total_sanitized == 3


# ---------------------------------------------------------------------------
# Case #2: 差分起動 (offset あり) → offset 以降のみ backfill
# ---------------------------------------------------------------------------


def test_case_02_incremental_backfill_only_past_offset(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    initial = [
        _make_assistant_entry("toolu_old"),
        # 既に cite 形式へ変換済みのエントリ (前回 backfill 済みを表す)
        _make_user_tool_result_entry("toolu_old", "older ref {{cite:M#1}} kept as-is"),
    ]
    _write_transcript(transcript, initial)

    state = HookState("sess-1")
    state.set_sanitize_offset(transcript.stat().st_size)

    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps(_make_assistant_entry("toolu_new"), ensure_ascii=False) + "\n")
        f.write(
            json.dumps(
                _make_user_tool_result_entry("toolu_new", "new ref D#1"),
                ensure_ascii=False,
            )
            + "\n"
        )

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    # 旧 entry の {{cite:M#1}} はそのまま (二重 cite 化されない)
    assert new_entries[1]["message"]["content"][0]["content"] == \
        "older ref {{cite:M#1}} kept as-is"
    # 新 entry の D#1 が変換される
    assert new_entries[3]["message"]["content"][0]["content"] == \
        "new ref {{cite:D#1}}"

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["extra"]["block_stats"]["sanitized"] == 1


# ---------------------------------------------------------------------------
# Case #3: transcript_path 空 → 何もしない exit 0、log なし、offset 未更新
# ---------------------------------------------------------------------------


def test_case_03_empty_transcript_path_no_op(fixture_db, state_dir):
    _, _, code = _run_hook({"session_id": "sess-1", "transcript_path": "", "cwd": "/tmp"})
    assert code == 0
    assert _read_citation_events(fixture_db) == []
    state = HookState("sess-1")
    assert state.get_sanitize_offset() == 0


# ---------------------------------------------------------------------------
# Case #4: offset > file size → リセットして全件再読み込み
# ---------------------------------------------------------------------------


def test_case_04_offset_exceeds_file_size_resets(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1 here"),
    ]
    _write_transcript(transcript, entries)

    state = HookState("sess-1")
    # transcript が縮んだ (古い session の offset が残っていた) 想定
    state.set_sanitize_offset(999_999)

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    assert new_entries[1]["message"]["content"][0]["content"] == \
        "ref {{cite:M#1}} here"
    assert state.get_sanitize_offset() == transcript.stat().st_size


# ---------------------------------------------------------------------------
# Case #5: tool_result 以外の entry (assistant text / user 入力) → 走査対象外
# ---------------------------------------------------------------------------


def test_case_05_non_tool_result_entries_untouched(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        # user の通常メッセージ (tool_result でない)
        {
            "type": "user",
            "uuid": "uuid-u1",
            "message": {"content": [{"type": "text", "text": "user said M#1 here"}]},
        },
        # assistant の text block (tool_use なし)
        {
            "type": "assistant",
            "uuid": "uuid-a1",
            "message": {"content": [{"type": "text", "text": "asst said D#1 here"}]},
        },
    ]
    _write_transcript(transcript, entries)
    original = transcript.read_bytes()

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    # 1 文字も書き換わっていない
    assert transcript.read_bytes() == original

    # tool_result block が1つも無いため、変化もイベント記録も発生しない
    assert _read_citation_events(fixture_db) == []


# ---------------------------------------------------------------------------
# Case #6: tool_result の content が cc-memory tool 由来でない → スキップ
# ---------------------------------------------------------------------------


def test_case_06_non_cc_memory_tool_result_skipped(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_read", tool_name="Read"),
        _make_user_tool_result_entry("toolu_read", "Read result mentions M#1"),
        _make_assistant_entry("toolu_cc"),
        _make_user_tool_result_entry("toolu_cc", "cc-memory M#1 ref"),
    ]
    _write_transcript(transcript, entries)

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    # Read tool 由来 → 変換しない
    assert new_entries[1]["message"]["content"][0]["content"] == \
        "Read result mentions M#1"
    # cc-memory tool 由来 → 変換
    assert new_entries[3]["message"]["content"][0]["content"] == \
        "cc-memory {{cite:M#1}} ref"

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["extra"]["block_stats"]["sanitized"] == 1


# ---------------------------------------------------------------------------
# Case #7: コードブロック / エスケープ / 既 cite はスキップ (PR-a 継承)
# ---------------------------------------------------------------------------


def test_case_07_skip_codeblock_escape_existing_cite(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    content = (
        "inline `M#1 code` outside D#1\n"
        "fence:\n```\nM#1 in fence\n```\nafter\n"
        "escape \\M#1 literal\n"
        "already {{cite:M#1}} cited"
    )
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", content),
    ]
    _write_transcript(transcript, entries)

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    out = new_entries[1]["message"]["content"][0]["content"]
    assert "`M#1 code`" in out  # inline backtick 維持
    assert "M#1 in fence" in out  # fence 内維持
    assert "\\M#1" in out  # エスケープ維持
    assert "{{cite:M#1}}" in out  # 既存 cite 維持
    assert "{{cite:D#1}}" in out  # 通常の D#1 のみ変換

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    # コードブロック等は occurrence に含まれるが sanitized されない。
    # sanitized=1 (D#1) + dangling=0 + skipped=4 (M#1 inline / fence / escape / existing cite) = 5
    assert events[0]["extra"]["block_stats"]["sanitized"] == 1
    assert events[0]["extra"]["block_stats"]["occurrence"] == 5


# ---------------------------------------------------------------------------
# Case #8: dangling target (DB 不在) → [deleted X#NNN]
# ---------------------------------------------------------------------------


def test_case_08_dangling_becomes_deleted_marker(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "known M#1, missing M#9999"),
    ]
    _write_transcript(transcript, entries)

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    out = new_entries[1]["message"]["content"][0]["content"]
    assert out == "known {{cite:M#1}}, missing [deleted M#9999]"

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    # dangling が1件以上含まれる場合、verification_result は 'dangling' (混在時の代表値)
    assert events[0]["verification_result"] == "dangling"
    assert events[0]["extra"]["block_stats"]["sanitized"] == 1
    assert events[0]["extra"]["block_stats"]["dangling"] == 1


# ---------------------------------------------------------------------------
# Case #9: sanitize_offset が正しく進む (idempotent)
# ---------------------------------------------------------------------------


def test_case_09_offset_progresses_idempotently(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1"),
    ]
    _write_transcript(transcript, entries)

    # 1 回目
    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0
    first_events = _read_citation_events(fixture_db)
    assert len(first_events) == 1
    assert first_events[0]["extra"]["block_stats"]["sanitized"] == 1
    first_offset = HookState("sess-1").get_sanitize_offset()

    # 2 回目 (差分なし) → 変化が無いので新規イベントは記録されない
    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0
    second_events = _read_citation_events(fixture_db)
    assert len(second_events) == 1
    assert HookState("sess-1").get_sanitize_offset() == first_offset


# ---------------------------------------------------------------------------
# Case #10: hook 内例外 → stderr warning + citation_event_log に failure イベント
#           記録 + exit 0 + offset 据え置き (次回再試行)
# ---------------------------------------------------------------------------


def test_case_10_exception_warns_logs_and_keeps_offset(fixture_db, state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1"),
    ]
    _write_transcript(transcript, entries)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sanitize_backfill_hook, "_sanitize_transcript_bytes", boom)

    _, stderr, code = _run_hook(_payload(str(transcript)))
    assert code == 0
    assert "[sanitize_backfill_hook]" in stderr

    # offset は更新されない (失敗時は据え置き)
    assert HookState("sess-1").get_sanitize_offset() == 0
    # スキャン/sanitize フェーズ例外も連続失敗カウンタを進める
    assert HookState("sess-1").get_sanitize_failure_count() == 1

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["before_text"] == ""
    assert events[0]["after_text"] == ""
    assert events[0]["verification_result"] is None
    assert events[0]["extra"]["error"] == "database is locked"


def test_case_10_repeated_scan_exceptions_trigger_skip(fixture_db, state_dir, tmp_path, monkeypatch):
    """同一例外が 3 回連続したら以降の SessionStart はスキップする (loop guard)。"""
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1"),
    ]
    _write_transcript(transcript, entries)

    def boom(*args, **kwargs):
        raise RuntimeError("schema mismatch")

    monkeypatch.setattr(sanitize_backfill_hook, "_sanitize_transcript_bytes", boom)

    for _ in range(3):
        _, _, code = _run_hook(_payload(str(transcript)))
        assert code == 0
    assert HookState("sess-1").get_sanitize_failure_count() == 3
    events_at_3 = _read_citation_events(fixture_db)
    assert len(events_at_3) == 3

    # 4 回目は loop guard で何もしない (event 件数据え置き)
    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0
    assert _read_citation_events(fixture_db) == events_at_3


# ---------------------------------------------------------------------------
# Case #11: CC_MEMORY_SANITIZE_DISABLE=1 → 即 exit 0、log なし、offset 未更新
# ---------------------------------------------------------------------------


def test_case_11_env_disable_short_circuits(fixture_db, state_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MEMORY_SANITIZE_DISABLE", "1")
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1"),
    ]
    _write_transcript(transcript, entries)
    original = transcript.read_bytes()

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0
    assert transcript.read_bytes() == original
    assert _read_citation_events(fixture_db) == []
    assert HookState("sess-1").get_sanitize_offset() == 0


# ---------------------------------------------------------------------------
# Case #12: cwd が cc-memory リポジトリ内 → 即 exit 0、log なし、offset 未更新
# ---------------------------------------------------------------------------


def test_case_12_cwd_in_cc_memory_repo_skipped(fixture_db, state_dir, tmp_path):
    repo_root = tmp_path / "cc-memory-repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "claude-code-memory"\nversion = "0.1.0"\n'
    )
    nested = repo_root / "src" / "deep"
    nested.mkdir(parents=True)

    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1"),
    ]
    _write_transcript(transcript, entries)
    original = transcript.read_bytes()

    _, _, code = _run_hook(_payload(str(transcript), cwd=str(nested)))
    assert code == 0
    assert transcript.read_bytes() == original
    assert _read_citation_events(fixture_db) == []


# ---------------------------------------------------------------------------
# Case #13: 大規模 transcript でも 600s 以内に完了 (perf スモーク)
# ---------------------------------------------------------------------------


def test_case_13_perf_under_threshold(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    entries = []
    # 1000 ペア (assistant tool_use + user tool_result) = 2000 行
    for i in range(1000):
        tool_id = f"toolu_{i:04d}"
        entries.append(_make_assistant_entry(tool_id))
        entries.append(_make_user_tool_result_entry(tool_id, f"row {i} ref M#1"))
    _write_transcript(transcript, entries)

    start = time.monotonic()
    _, _, code = _run_hook(_payload(str(transcript)))
    elapsed = time.monotonic() - start

    assert code == 0
    assert elapsed < 10.0, f"hook took too long: {elapsed:.2f}s"

    events = _read_citation_events(fixture_db)
    # 1000 block 全てが変化したので block 単位で 1000 イベント
    assert len(events) == 1000
    assert sum(e["extra"]["block_stats"]["sanitized"] for e in events) == 1000


# ---------------------------------------------------------------------------
# Case #14: 書き戻し時に entry の他フィールド (uuid / timestamp 等) が維持される
# ---------------------------------------------------------------------------


def test_case_14_other_entry_fields_preserved(fixture_db, state_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    assistant = _make_assistant_entry("toolu_01")
    user_entry = _make_user_tool_result_entry(
        "toolu_01",
        "ref M#1",
        extra={"parentUuid": "parent-xyz", "sessionId": "sess-orig"},
    )
    _write_transcript(transcript, [assistant, user_entry])

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0

    new_entries = _read_transcript_lines(transcript)
    assert new_entries[1]["uuid"] == user_entry["uuid"]
    assert new_entries[1]["timestamp"] == user_entry["timestamp"]
    assert new_entries[1]["parentUuid"] == "parent-xyz"
    assert new_entries[1]["sessionId"] == "sess-orig"
    assert new_entries[1]["message"]["content"][0]["tool_use_id"] == "toolu_01"
    assert new_entries[1]["message"]["content"][0]["content"] == "ref {{cite:M#1}}"


# ---------------------------------------------------------------------------
# Case #15: atomic write — tmpfile + rename + harness 並行 append 検出
# ---------------------------------------------------------------------------


def test_case_15_write_back_detects_mtime_mismatch(tmp_path):
    """書き戻し直前の mtime 再確認で並行 append を検出し書き戻しを中断する。"""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"original content line\n")
    real_mtime = transcript.stat().st_mtime

    result = sanitize_backfill_hook._write_back_transcript(
        transcript, b"new content\n", real_mtime - 100.0, int(time.time())
    )
    assert result == "harness_race"
    assert transcript.read_bytes() == b"original content line\n"
    # tmpfile / backup は残らない
    leftover = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name or ".bak" in p.name]
    assert leftover == [], f"leftover files: {leftover}"


def test_case_15_write_back_success_uses_atomic_rename(tmp_path):
    """成功時に transcript が atomic に置き換えられ、tmpfile / backup が残らない。"""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"original\n")
    real_mtime = transcript.stat().st_mtime

    result = sanitize_backfill_hook._write_back_transcript(
        transcript, b"sanitized\n", real_mtime, int(time.time())
    )
    assert result is None
    assert transcript.read_bytes() == b"sanitized\n"
    leftover = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name or ".bak" in p.name]
    assert leftover == [], f"leftover files: {leftover}"


def test_case_15_harness_race_recorded_as_failure_event(
    fixture_db, state_dir, tmp_path, monkeypatch
):
    """main() 経由で harness_race を検出した場合 citation_event_log に failure 記録 + offset 据え置き。"""
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        _make_assistant_entry("toolu_01"),
        _make_user_tool_result_entry("toolu_01", "ref M#1"),
    ]
    _write_transcript(transcript, entries)
    original_bytes = transcript.read_bytes()

    def fake_write_back(*_args, **_kwargs):
        return "harness_race"

    monkeypatch.setattr(sanitize_backfill_hook, "_write_back_transcript", fake_write_back)

    _, _, code = _run_hook(_payload(str(transcript)))
    assert code == 0
    assert transcript.read_bytes() == original_bytes

    events = _read_citation_events(fixture_db)
    assert len(events) == 1
    assert events[0]["before_text"] == ""
    assert events[0]["after_text"] == ""
    assert events[0]["verification_result"] is None
    assert events[0]["extra"]["failure_reason"] == "harness_race"
    assert HookState("sess-1").get_sanitize_offset() == 0
