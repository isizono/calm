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


def test_record_signal_dedup_updates_refs_context_session_id(temp_db):
    """dedup 時に refs / context / session_id が最新 occurrence の値で上書きされる。"""
    r1 = ss.record_signal(
        "contradiction",
        "X contradicts Y",
        source="agent",
        refs=[{"type": "decision", "id": 1}, {"type": "decision", "id": 2}],
        context={"resolution": "old_correct"},
        session_id="sess-1",
    )
    r2 = ss.record_signal(
        "contradiction",
        "X contradicts Y",
        source="agent",
        refs=[{"type": "decision", "id": 1}, {"type": "decision", "id": 3}],
        context={"resolution": "new_correct"},
        session_id="sess-2",
    )

    assert r2["id"] == r1["id"]
    assert r2["deduped"] is True

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM signal_events WHERE id = ?", (r1["id"],)
        ).fetchone()
    finally:
        conn.close()
    signal = ss._signal_row_to_dict(row)
    assert signal["refs"] == [{"type": "decision", "id": 1}, {"type": "decision", "id": 3}]
    assert signal["context"] == {"resolution": "new_correct"}
    assert signal["session_id"] == "sess-2"


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

    def test_status_and_kind_filter_combined(self, temp_db):
        r1 = ss.record_signal("machine_error", "a", source="s1")
        ss.update_signal(r1["id"], "dismissed")  # machine_error / dismissed
        ss.record_signal("machine_error", "b", source="s2")  # machine_error / new
        ss.record_signal("friction", "c", source="s3")  # friction / new

        result = ss.get_signals(status="new", kind="machine_error")

        assert result["total_count"] == 1
        assert result["signals"][0]["summary"] == "b"
        assert result["signals"][0]["kind"] == "machine_error"
        assert result["signals"][0]["status"] == "new"

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

    def test_response_hides_raw_session_id_and_fingerprint(self, temp_db):
        """session_id/fingerprintは記録側の内部専用フィールドで、レスポンスに現れない。"""
        ss.record_signal("machine_error", "boom", source="tool:foo", session_id="sess-1")

        result = ss.get_signals(status=None)

        signal = result["signals"][0]
        assert "session_id" not in signal
        assert "fingerprint" not in signal

    def test_response_uses_id_raw_not_bare_id(self, temp_db):
        """idは他のget系ツールと同じreadable_id変換でid_rawに退避され、idキーは残らない。"""
        r1 = ss.record_signal("machine_error", "boom", source="tool:foo")

        result = ss.get_signals(status=None)

        signal = result["signals"][0]
        assert "id" not in signal
        assert signal["id_raw"] == r1["id"]

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

    def test_response_hides_raw_session_id_fingerprint_and_bare_id(self, temp_db):
        """get_signalsと同様、内部識別子はレスポンスに現れずidはid_rawで返る。"""
        r1 = ss.record_signal("friction", "a", session_id="sess-1")

        result = ss.update_signal(r1["id"], "triaged")

        signal = result["signal"]
        assert "session_id" not in signal
        assert "fingerprint" not in signal
        assert "id" not in signal
        assert signal["id_raw"] == r1["id"]

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

    def _set_last_seen_at(self, signal_id, value):
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE signal_events SET last_seen_at = ? WHERE id = ?",
                (value, signal_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _read_last_seen_at(self, signal_id):
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT last_seen_at FROM signal_events WHERE id = ?", (signal_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_status_transition_does_not_touch_last_seen_at(self, temp_db):
        r1 = ss.record_signal("friction", "a")
        self._set_last_seen_at(r1["id"], "2020-01-01 00:00:00")

        ss.update_signal(r1["id"], "dismissed")

        assert self._read_last_seen_at(r1["id"]) == "2020-01-01 00:00:00"

    def test_promote_does_not_touch_last_seen_at(self, temp_db):
        from src.services.topic_service import add_topic

        topic = add_topic(title="t", description="d", tags=["domain:test"])
        r1 = ss.record_signal("precedent_miss", "missed something")
        self._set_last_seen_at(r1["id"], "2020-01-01 00:00:00")

        ss.update_signal(
            r1["id"], "promoted", promoted_type="topic", promoted_id=topic["topic_id"]
        )

        assert self._read_last_seen_at(r1["id"]) == "2020-01-01 00:00:00"

    def test_preserves_existing_promotion_when_omitted(self, temp_db):
        from src.services.topic_service import add_topic

        topic = add_topic(title="t", description="d", tags=["domain:test"])
        r1 = ss.record_signal("precedent_miss", "missed something")
        ss.update_signal(r1["id"], "promoted", promoted_type="topic", promoted_id=topic["topic_id"])

        result = ss.update_signal(r1["id"], "dismissed")

        assert result["signal"]["status"] == "dismissed"
        assert result["signal"]["promoted_type"] == "topic"
        assert result["signal"]["promoted_id"] == topic["topic_id"]
