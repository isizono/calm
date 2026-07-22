"""relay_diagnostics_service.outbox_status() の unit test。

relay_outbox 行の pending/delivered/dead 判定と、outbox_id のバリデーション、
DLQ 物理削除後の not_found メッセージが実際の設定値と一致することを検証する。
"""
from src.db import get_connection
from relay_sdk import config as sdk_config
from src.services.relay import diagnostics


def _insert_row(
    *,
    labels='["decision:1"]',
    title="見出し",
    created_at="2026-07-01T00:00:00Z",
    processed_at=None,
    retry_count=0,
    last_error=None,
    dead_at=None,
):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO relay_outbox"
            " (ref_type, ref_id, labels, title, idempotency_key, created_at,"
            "  processed_at, retry_count, last_error, dead_at)"
            " VALUES ('publish', 'body', ?, ?, 'idem-1', ?, ?, ?, ?, ?)",
            (labels, title, created_at, processed_at, retry_count, last_error, dead_at),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


class TestOutboxStatusPending:
    def test_pending_row_has_null_processed_and_dead(self, temp_db):
        outbox_id = _insert_row()
        result = diagnostics.outbox_status(outbox_id)
        assert result["status"] == "pending"
        assert result["processed_at"] is None
        assert result["dead_at"] is None
        assert result["outbox_id"] == outbox_id
        assert result["labels"] == ["decision:1"]


class TestOutboxStatusDelivered:
    def test_processed_row_is_delivered(self, temp_db):
        outbox_id = _insert_row(processed_at="2026-07-01T00:01:00Z")
        result = diagnostics.outbox_status(outbox_id)
        assert result["status"] == "delivered"
        assert result["processed_at"] == "2026-07-01T00:01:00Z"


class TestOutboxStatusDead:
    def test_dead_row_keeps_retry_count_and_last_error(self, temp_db):
        outbox_id = _insert_row(
            retry_count=5, last_error="boom", dead_at="2026-07-02T00:00:00Z"
        )
        result = diagnostics.outbox_status(outbox_id)
        assert result["status"] == "dead"
        assert result["retry_count"] == 5
        assert result["last_error"] == "boom"
        assert result["dead_at"] == "2026-07-02T00:00:00Z"


class TestOutboxStatusNotFound:
    def test_missing_id_returns_not_found_with_dlq_days_from_config(self, temp_db):
        result = diagnostics.outbox_status(999999)
        assert result["error"]["code"] == "not_found"
        assert str(sdk_config.DLQ_PHYSICAL_DELETE_DAYS) in result["error"]["message"]


class TestOutboxStatusValidation:
    def test_zero_is_rejected(self, temp_db):
        result = diagnostics.outbox_status(0)
        assert result["error"]["code"] == "validation"

    def test_negative_is_rejected(self, temp_db):
        result = diagnostics.outbox_status(-1)
        assert result["error"]["code"] == "validation"

    def test_string_is_rejected(self, temp_db):
        result = diagnostics.outbox_status("1")
        assert result["error"]["code"] == "validation"

    def test_bool_true_is_rejected(self, temp_db):
        """bool は int のサブクラスであるため isinstance だけでは通ってしまう落とし穴を塞ぐ。"""
        result = diagnostics.outbox_status(True)
        assert result["error"]["code"] == "validation"

    def test_bool_false_is_rejected(self, temp_db):
        result = diagnostics.outbox_status(False)
        assert result["error"]["code"] == "validation"


class TestOutboxStatusOmitted:
    def test_none_returns_none(self, temp_db):
        assert diagnostics.outbox_status(None) is None
