"""scripts/precedent_scan.py のテスト。

read-only（書き込みクエリを発行しない）であることと、節あり件数・warning分布・
アンカー付き件数のレポート内容を検証する。
"""
import json
import os
import sqlite3
import tempfile

import pytest

from src.db import init_database
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision, retract_decision

from scripts.precedent_scan import (
    _categorize_warning,
    _open_readonly_connection,
    main,
    render_text_report,
    scan_precedents,
)

DEFAULT_TAGS = ["domain:test"]

PRECEDENT_REASON = (
    "自由記述の理由。\n"
    "\n"
    "却下案:\n"
    "- 案A: 理由A\n"
    "\n"
    "検証: 実機確認 / 2026-07-04\n"
)

PLAIN_REASON = "普通の理由本文。節は無い。"

NEAR_MISS_REASON = "却下例:\n- 案A: 理由A\n"

NO_ANCHOR_SECTIONED_REASON = "適用条件:\n- 対象領域\n"


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic(temp_db):
    return add_topic(title="scanテスト", description="テスト用", tags=DEFAULT_TAGS)


class TestScanPrecedentsCounts:
    def test_counts_sections_anchors_and_warnings(self, topic, temp_db):
        tid = topic["topic_id"]
        add_decision("d1", PRECEDENT_REASON, topic_id=tid, tags=DEFAULT_TAGS)
        add_decision("d2", PLAIN_REASON, topic_id=tid, tags=DEFAULT_TAGS)
        add_decision("d3", NEAR_MISS_REASON, topic_id=tid, tags=DEFAULT_TAGS)
        add_decision("d4", NO_ANCHOR_SECTIONED_REASON, topic_id=tid, tags=DEFAULT_TAGS)

        conn = _open_readonly_connection(temp_db)
        try:
            report = scan_precedents(conn)
        finally:
            conn.close()

        assert report["total_decisions"] == 4
        # d1, d3, d4 に節（正規 or 近似見出し）がある。d2 は legacy 本文
        assert report["with_sections"] == 3
        assert report["without_sections"] == 1
        # 検証アンカーがあるのは d1 のみ
        assert report["with_verification_anchor"] == 1
        # d3 に近似見出しwarningが1件
        assert report["warnings_total"] >= 1
        assert report["warning_counts"].get("near_miss_heading") == 1

    def test_excludes_retracted_decisions(self, topic, temp_db):
        tid = topic["topic_id"]
        created = add_decision("d1", PRECEDENT_REASON, topic_id=tid, tags=DEFAULT_TAGS)
        add_decision("d2", PLAIN_REASON, topic_id=tid, tags=DEFAULT_TAGS)
        retract_decision(created["decision_id"])

        conn = _open_readonly_connection(temp_db)
        try:
            report = scan_precedents(conn)
        finally:
            conn.close()

        # 取り消し済みのd1は集計対象から除外される
        assert report["total_decisions"] == 1
        assert report["with_sections"] == 0

    def test_empty_db_returns_zero_counts(self, temp_db):
        conn = _open_readonly_connection(temp_db)
        try:
            report = scan_precedents(conn)
        finally:
            conn.close()

        assert report["total_decisions"] == 0
        assert report["with_sections"] == 0
        assert report["without_sections"] == 0
        assert report["with_verification_anchor"] == 0
        assert report["warnings_total"] == 0
        assert report["warning_counts"] == {}


class TestReadOnlyConnection:
    def test_readonly_connection_rejects_writes(self, temp_db):
        conn = _open_readonly_connection(temp_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                    ("書き込みテスト", "reason"),
                )
        finally:
            conn.close()

    def test_readonly_connection_allows_reads(self, topic, temp_db):
        tid = topic["topic_id"]
        add_decision("d1", PLAIN_REASON, topic_id=tid, tags=DEFAULT_TAGS)

        conn = _open_readonly_connection(temp_db)
        try:
            rows = conn.execute("SELECT COUNT(*) AS c FROM decisions").fetchall()
            assert rows[0]["c"] == 1
        finally:
            conn.close()


class TestCategorizeWarning:
    def test_known_prefixes_mapped(self):
        assert _categorize_warning("empty section: 却下案:") == "empty_section"
        assert (
            _categorize_warning("near-miss heading '却下例:' is not a recognized ...")
            == "near_miss_heading"
        )
        assert (
            _categorize_warning("verification anchor without date: '実機確認'")
            == "verification_anchor_without_date"
        )
        assert (
            _categorize_warning("rejected alternative without ': ' separator: '案A'")
            == "rejected_alternative_without_separator"
        )

    def test_unknown_prefix_falls_back_to_other(self):
        assert _categorize_warning("some unforeseen future warning text") == "other"


class TestRenderTextReport:
    def test_report_contains_key_metrics(self):
        report = {
            "total_decisions": 10,
            "with_sections": 4,
            "without_sections": 6,
            "with_verification_anchor": 2,
            "warnings_total": 3,
            "warning_counts": {"near_miss_heading": 2, "other": 1},
        }
        text = render_text_report(report)
        assert "定型節あり" in text
        assert "定型節なし" in text
        assert "検証アンカー付き" in text
        assert "near_miss_heading" in text

    def test_report_handles_zero_total_without_error(self):
        report = {
            "total_decisions": 0,
            "with_sections": 0,
            "without_sections": 0,
            "with_verification_anchor": 0,
            "warnings_total": 0,
            "warning_counts": {},
        }
        text = render_text_report(report)
        assert "n/a" in text


class TestCliMain:
    def test_json_format_output(self, topic, temp_db, capsys):
        tid = topic["topic_id"]
        add_decision("d1", PRECEDENT_REASON, topic_id=tid, tags=DEFAULT_TAGS)

        exit_code = main(["--db-path", temp_db, "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["total_decisions"] == 1
        assert payload["with_sections"] == 1

    def test_text_format_output(self, topic, temp_db, capsys):
        tid = topic["topic_id"]
        add_decision("d1", PLAIN_REASON, topic_id=tid, tags=DEFAULT_TAGS)

        exit_code = main(["--db-path", temp_db, "--format", "text"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "判例定型節" in captured.out
