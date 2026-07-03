"""signal_service の単体テスト

signal_events テーブルへの record/dedup/get/update と、捕捉専用の
capture_signal_safe が例外を外に漏らさないことを検証する。
"""
import sqlite3

import pytest

from src.db import get_connection
from src.services import signal_service as ss


def test_record_signal_creates_row(temp_db):
    result = ss.record_signal("machine_error", "boom", source="tool:foo", detail="tb")
    assert result["deduped"] is False
    assert result["occurrence_count"] == 1

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM signal_events WHERE id = ?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["kind"] == "machine_error"
    assert row["source"] == "tool:foo"
    assert row["summary"] == "boom"
    assert row["status"] == "new"
    assert row["occurrence_count"] == 1


def test_record_signal_rejects_unknown_kind(temp_db):
    with pytest.raises(ValueError):
        ss.record_signal("not_a_real_kind", "boom")


def test_record_signal_rejects_empty_summary(temp_db):
    with pytest.raises(ValueError):
        ss.record_signal("friction", "   ")


def test_record_signal_rejects_unserializable_refs(temp_db):
    class Unserializable:
        pass

    with pytest.raises(ValueError):
        ss.record_signal(
            "contradiction", "x", refs=[{"type": "decision", "id": Unserializable()}]
        )


def test_record_signal_dedups_same_fingerprint(temp_db):
    r1 = ss.record_signal("machine_error", "Something broke", source="tool:foo")
    r2 = ss.record_signal("machine_error", "  something   BROKE  ", source="tool:foo", detail="second")

    assert r2["id"] == r1["id"]
    assert r2["deduped"] is True
    assert r2["occurrence_count"] == 2

    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM signal_events").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 2
    assert rows[0]["detail"] == "second"


def test_record_signal_does_not_dedup_after_triage(temp_db):
    r1 = ss.record_signal("machine_error", "Something broke", source="tool:foo")
    ss.update_signal(r1["id"], "triaged")

    r2 = ss.record_signal("machine_error", "Something broke", source="tool:foo")

    assert r2["id"] != r1["id"]
    assert r2["deduped"] is False
    assert r2["occurrence_count"] == 1

    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]
    finally:
        conn.close()
    assert total == 2


def test_record_signal_different_kind_or_source_not_deduped(temp_db):
    r1 = ss.record_signal("machine_error", "same text", source="tool:foo")
    r2 = ss.record_signal("friction", "same text", source="tool:foo")
    r3 = ss.record_signal("machine_error", "same text", source="tool:bar")

    assert len({r1["id"], r2["id"], r3["id"]}) == 3


def test_capture_signal_safe_never_raises_on_invalid_kind(temp_db):
    ss.capture_signal_safe("not_a_real_kind", "boom")  # 例外を投げないことのみ検証

    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]
    finally:
        conn.close()
    assert total == 0


def test_capture_signal_safe_writes_valid_signal(temp_db):
    ss.capture_signal_safe("machine_error", "boom", source="hook:session_start")

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events").fetchone()
    finally:
        conn.close()
    assert row["kind"] == "machine_error"
    assert row["source"] == "hook:session_start"


def test_capture_signal_safe_survives_db_failure(temp_db, monkeypatch):
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ss, "record_signal", _boom)
    ss.capture_signal_safe("machine_error", "boom")  # 例外を投げないことのみ検証


class TestGetSignals:
    def test_default_filters_to_new_status(self, temp_db):
        r1 = ss.record_signal("machine_error", "a", source="s1")
        ss.update_signal(r1["id"], "dismissed")
        ss.record_signal("friction", "b", source="s2")

        result = ss.get_signals()

        assert result["total_count"] == 1
        assert result["signals"][0]["kind"] == "friction"

    def test_status_none_returns_all(self, temp_db):
        r1 = ss.record_signal("machine_error", "a", source="s1")
        ss.update_signal(r1["id"], "dismissed")
        ss.record_signal("friction", "b", source="s2")

        result = ss.get_signals(status=None)

        assert result["total_count"] == 2

    def test_kind_filter(self, temp_db):
        ss.record_signal("machine_error", "a", source="s1")
        ss.record_signal("friction", "b", source="s2")

        result = ss.get_signals(status=None, kind="friction")

        assert result["total_count"] == 1
        assert result["signals"][0]["kind"] == "friction"

    def test_invalid_status_returns_validation_error(self, temp_db):
        result = ss.get_signals(status="not_a_status")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_kind_returns_validation_error(self, temp_db):
        result = ss.get_signals(kind="not_a_kind")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_refs_and_context_are_deserialized(self, temp_db):
        ss.record_signal(
            "contradiction",
            "x vs y",
            refs=[{"type": "decision", "id": 1}],
            context={"resolution": "new_correct"},
        )

        result = ss.get_signals(status=None)

        signal = result["signals"][0]
        assert signal["refs"] == [{"type": "decision", "id": 1}]
        assert signal["context"] == {"resolution": "new_correct"}

    def test_include_stats_cross_tabulates_kind_and_status(self, temp_db):
        r1 = ss.record_signal("machine_error", "a", source="s1")
        ss.update_signal(r1["id"], "promoted", promoted_type=None, promoted_id=None)
        ss.record_signal("friction", "b", source="s2")
        ss.record_signal("friction", "c", source="s3")

        result = ss.get_signals(status=None, include_stats=True)

        assert result["stats"]["by_kind_status"]["friction"]["new"] == 2
        assert result["stats"]["by_kind_status"]["machine_error"]["promoted"] == 1
        assert result["stats"]["last_30d"]["friction"] == 2

    def test_limit_is_clamped_to_max(self, temp_db):
        for i in range(3):
            ss.record_signal("friction", f"item {i}", source=f"s{i}")

        result = ss.get_signals(status=None, limit=99999)

        assert result["total_count"] == 3
        assert len(result["signals"]) == 3


class TestUpdateSignal:
    def test_status_transition_without_promotion(self, temp_db):
        r1 = ss.record_signal("friction", "a")

        result = ss.update_signal(r1["id"], "triaged")

        assert result["signal"]["status"] == "triaged"
        assert result["signal"]["promoted_type"] is None

    def test_promote_links_existing_entity(self, temp_db):
        from src.services.topic_service import add_topic

        topic = add_topic(title="t", description="d", tags=["domain:test"])
        r1 = ss.record_signal("precedent_miss", "missed something")

        result = ss.update_signal(
            r1["id"], "promoted", promoted_type="topic", promoted_id=topic["topic_id"]
        )

        assert result["signal"]["status"] == "promoted"
        assert result["signal"]["promoted_type"] == "topic"
        assert result["signal"]["promoted_id"] == topic["topic_id"]

    def test_promote_rejects_nonexistent_entity(self, temp_db):
        r1 = ss.record_signal("precedent_miss", "missed something")

        result = ss.update_signal(
            r1["id"], "promoted", promoted_type="topic", promoted_id=999999
        )

        assert result["error"]["code"] == "NOT_FOUND"

    def test_partial_promoted_args_rejected(self, temp_db):
        r1 = ss.record_signal("friction", "a")

        result = ss.update_signal(r1["id"], "triaged", promoted_type="topic")

        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_promoted_type_rejected(self, temp_db):
        r1 = ss.record_signal("friction", "a")

        result = ss.update_signal(
            r1["id"], "triaged", promoted_type="tag", promoted_id=1
        )

        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_status_rejected(self, temp_db):
        r1 = ss.record_signal("friction", "a")

        result = ss.update_signal(r1["id"], "not_a_status")

        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_nonexistent_signal_returns_not_found(self, temp_db):
        result = ss.update_signal(999999, "triaged")
        assert result["error"]["code"] == "NOT_FOUND"

    def test_preserves_existing_promotion_when_omitted(self, temp_db):
        from src.services.topic_service import add_topic

        topic = add_topic(title="t", description="d", tags=["domain:test"])
        r1 = ss.record_signal("precedent_miss", "missed something")
        ss.update_signal(r1["id"], "promoted", promoted_type="topic", promoted_id=topic["topic_id"])

        result = ss.update_signal(r1["id"], "dismissed")

        assert result["signal"]["status"] == "dismissed"
        assert result["signal"]["promoted_type"] == "topic"
        assert result["signal"]["promoted_id"] == topic["topic_id"]
