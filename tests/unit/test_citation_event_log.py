"""hooks/citation_event_log.py のユニットテスト。

sanitize_tool_result_hook / sanitize_backfill_hook が共有する
citation_event_log INSERT ヘルパーの契約 (source / verification_result の
バリデーション、バッチ INSERT の部分スキップ) を検証する。
"""
import json
import os
import sqlite3
import tempfile

import pytest

from hooks import citation_event_log

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
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.db")
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_CITATION_EVENT_LOG_DDL)
            conn.commit()
        finally:
            conn.close()
        yield path


def _read_events(path: str) -> list[dict]:
    conn = sqlite3.connect(path)
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


class TestLogEvent:
    def test_inserts_row_with_valid_values(self, db_path):
        citation_event_log.log_event(
            db_path,
            source="transcript_post_tool_use",
            hook_label="test_hook",
            session_id="sess-1",
            transcript_path="/tmp/t.jsonl",
            tool_name="check_in",
            before_text="M#1",
            after_text="{{cite:M#1}}",
            verification_result="exists",
            extra={"sanitized_count": 1},
        )
        events = _read_events(db_path)
        assert len(events) == 1
        assert events[0]["source"] == "transcript_post_tool_use"
        assert events[0]["tool_name"] == "check_in"
        assert events[0]["before_text"] == "M#1"
        assert events[0]["after_text"] == "{{cite:M#1}}"
        assert events[0]["verification_result"] == "exists"
        assert events[0]["extra"]["session_id"] == "sess-1"
        assert events[0]["extra"]["transcript_path"] == "/tmp/t.jsonl"
        assert events[0]["extra"]["sanitized_count"] == 1

    def test_skips_invalid_source(self, db_path, capsys):
        citation_event_log.log_event(
            db_path,
            source="not_a_real_source",
            hook_label="test_hook",
            session_id=None,
            transcript_path=None,
            tool_name=None,
            before_text="x",
            after_text="y",
            verification_result="exists",
            extra={},
        )
        assert _read_events(db_path) == []
        assert "test_hook" in capsys.readouterr().err

    def test_skips_invalid_verification_result(self, db_path, capsys):
        citation_event_log.log_event(
            db_path,
            source="transcript_post_tool_use",
            hook_label="test_hook",
            session_id=None,
            transcript_path=None,
            tool_name=None,
            before_text="x",
            after_text="y",
            verification_result="not_a_real_result",
            extra={},
        )
        assert _read_events(db_path) == []
        assert "test_hook" in capsys.readouterr().err

    def test_allows_none_verification_result(self, db_path):
        """failure イベント記録経路 (verification_result=None) は許可される。"""
        citation_event_log.log_event(
            db_path,
            source="transcript_session_start_backfill",
            hook_label="test_hook",
            session_id=None,
            transcript_path=None,
            tool_name=None,
            before_text="",
            after_text="",
            verification_result=None,
            extra={"error": "boom"},
        )
        events = _read_events(db_path)
        assert len(events) == 1
        assert events[0]["verification_result"] is None
        assert events[0]["extra"]["error"] == "boom"


class TestLogEventsBatch:
    def test_inserts_multiple_rows_in_one_call(self, db_path):
        citation_event_log.log_events_batch(
            db_path,
            source="transcript_session_start_backfill",
            hook_label="test_hook",
            session_id="sess-1",
            transcript_path="/tmp/t.jsonl",
            events=[
                {
                    "tool_name": "check_in",
                    "before_text": "M#1",
                    "after_text": "{{cite:M#1}}",
                    "verification_result": "exists",
                    "stats": {"sanitized": 1, "dangling": 0, "occurrence": 1},
                },
                {
                    "tool_name": "check_in",
                    "before_text": "M#9999",
                    "after_text": "[deleted M#9999]",
                    "verification_result": "dangling",
                    "stats": {"sanitized": 0, "dangling": 1, "occurrence": 1},
                },
            ],
        )
        events = _read_events(db_path)
        assert len(events) == 2
        assert events[0]["after_text"] == "{{cite:M#1}}"
        assert events[0]["extra"]["block_stats"]["sanitized"] == 1
        assert events[1]["after_text"] == "[deleted M#9999]"
        assert events[1]["verification_result"] == "dangling"

    def test_noop_on_empty_events(self, db_path):
        citation_event_log.log_events_batch(
            db_path,
            source="transcript_session_start_backfill",
            hook_label="test_hook",
            session_id=None,
            transcript_path=None,
            events=[],
        )
        assert _read_events(db_path) == []

    def test_skips_only_invalid_event_keeps_valid_ones(self, db_path, capsys):
        """1件だけ verification_result が不正な event は個別にスキップし、

        残りの正常な event は INSERT される。
        """
        citation_event_log.log_events_batch(
            db_path,
            source="transcript_session_start_backfill",
            hook_label="test_hook",
            session_id=None,
            transcript_path=None,
            events=[
                {
                    "tool_name": "check_in",
                    "before_text": "M#1",
                    "after_text": "{{cite:M#1}}",
                    "verification_result": "exists",
                    "stats": {},
                },
                {
                    "tool_name": "check_in",
                    "before_text": "bad",
                    "after_text": "bad",
                    "verification_result": "not_a_real_result",
                    "stats": {},
                },
            ],
        )
        events = _read_events(db_path)
        assert len(events) == 1
        assert events[0]["before_text"] == "M#1"
        assert "test_hook" in capsys.readouterr().err

    def test_skips_all_when_source_invalid(self, db_path):
        citation_event_log.log_events_batch(
            db_path,
            source="not_a_real_source",
            hook_label="test_hook",
            session_id=None,
            transcript_path=None,
            events=[
                {
                    "tool_name": "check_in",
                    "before_text": "M#1",
                    "after_text": "{{cite:M#1}}",
                    "verification_result": "exists",
                    "stats": {},
                }
            ],
        )
        assert _read_events(db_path) == []
