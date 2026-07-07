"""relay_status MCPツール（main.py配線）のユニットテスト。

outbox_status()/health_snapshot() 単体の分岐は
tests/unit/test_relay_diagnostics_service.py・tests/unit/test_relay_runtime.py が担う。
本ファイルは main.py 側の配線（get_relay_runtime の None 安全性、outbox エラーの
早期return、runtime インスタンス有無による切り替え）を検証する。
"""
import src.main as main_module
from src.db import get_connection
from src.main import get_relay_runtime, relay_status
from src.services.relay.runtime import RelayRuntime


def _insert_outbox_row(
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


class TestGetRelayRuntimeNoneSafety:
    def test_returns_none_without_nameerror_when_unset(self):
        """stdio transport / remote プロセス相当（__main__ 未実行）では None を返す。"""
        assert get_relay_runtime() is None


class TestRelayStatusOutboxOmitted:
    def test_outbox_is_none_and_runtime_section_present(self, temp_db, monkeypatch):
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        result = relay_status()
        assert result["outbox"] is None
        assert result["runtime"] == {
            "configured": False,
            "running": False,
            "threads": {},
        }


class TestRelayStatusOutboxNotFound:
    def test_not_found_error_is_returned_without_runtime_key(self, temp_db):
        result = relay_status(outbox_id=999999)
        assert result["error"]["code"] == "not_found"
        assert "runtime" not in result
        assert "outbox" not in result


class TestRelayStatusOutboxValidation:
    def test_validation_error_is_returned_without_runtime_key(self, temp_db):
        result = relay_status(outbox_id=-1)
        assert result["error"]["code"] == "validation"
        assert "runtime" not in result


class TestRelayStatusOutboxAndRuntimeTogether:
    def test_existing_outbox_row_and_runtime_are_both_returned(self, temp_db, monkeypatch):
        """outbox_id指定時、実在する行の配送状況とruntimeセクションが両方組み合わさって返る。"""
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        outbox_id = _insert_outbox_row(processed_at="2026-07-01T00:01:00Z")

        result = relay_status(outbox_id=outbox_id)

        assert result["outbox"] == {
            "outbox_id": outbox_id,
            "status": "delivered",
            "labels": ["decision:1"],
            "title": "見出し",
            "created_at": "2026-07-01T00:00:00Z",
            "processed_at": "2026-07-01T00:01:00Z",
            "dead_at": None,
            "retry_count": 0,
            "last_error": None,
        }
        assert result["runtime"] == {
            "configured": False,
            "running": False,
            "threads": {},
        }


class TestRelayStatusUsesLiveRuntimeInstance:
    def test_running_runtime_instance_is_reflected_in_response(self, temp_db, monkeypatch):
        """_relay_runtime が設定されている場合、health_snapshot() の値をそのまま返す。"""
        runtime = RelayRuntime(active_sessions_getter=lambda: set())
        monkeypatch.setattr(main_module, "_relay_runtime", runtime)
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.setattr(runtime, "_run_intake", lambda: runtime._stop_event.wait())
        monkeypatch.setattr(runtime, "_run_lease_loop", lambda: runtime._stop_event.wait())
        monkeypatch.setattr(runtime, "_run_dispatcher", lambda: runtime._stop_event.wait())
        assert runtime.start() is True
        try:
            result = relay_status()
            assert result["runtime"]["running"] is True
            assert set(result["runtime"]["threads"].keys()) == {
                "relay-intake",
                "relay-lease-loop",
                "relay-dispatcher",
            }
        finally:
            runtime.stop()
