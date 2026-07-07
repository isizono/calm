"""report_signal / get_signals / update_signal MCPツールのユニットテスト

src.main 経由の統合的な挙動 (validationエラーのdict化、capability gating) を
検証する。record_signal/get_signals/update_signalの詳細な分岐は
tests/unit/test_signal_service.py が担う。
"""
import pytest

from src.main import report_signal, get_signals, update_signal
from src.services.guard_service import CapabilityError


class TestReportSignalTool:
    def test_records_and_returns_id(self, temp_db):
        result = report_signal("machine_error", "boom")
        assert "id" in result
        assert result["deduped"] is False

    def test_invalid_kind_returns_validation_error_dict_not_raise(self, temp_db):
        result = report_signal("not_a_kind", "boom")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_summary_returns_validation_error_dict_not_raise(self, temp_db):
        result = report_signal("friction", "")
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestGetSignalsTool:
    def test_returns_reported_signal(self, temp_db):
        report_signal("friction", "使いにくい")

        result = get_signals()

        assert result["total_count"] == 1
        assert result["signals"][0]["summary"] == "使いにくい"

    def test_does_not_expose_internal_identifiers(self, temp_db):
        """session_id/fingerprintは露出せず、idはid_rawのreadable形式で返る。"""
        created = report_signal("friction", "使いにくい")

        result = get_signals()

        signal = result["signals"][0]
        assert "session_id" not in signal
        assert "fingerprint" not in signal
        assert "id" not in signal
        assert signal["id_raw"] == created["id"]


class TestUpdateSignalTool:
    def test_orch_can_update(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "orch")
        created = report_signal("friction", "a")

        result = update_signal(created["id"], "triaged")

        assert result["signal"]["status"] == "triaged"

    def test_worker_is_blocked(self, temp_db, monkeypatch):
        created = report_signal("friction", "a")
        monkeypatch.setenv("OW_ROLE", "worker")

        with pytest.raises(CapabilityError):
            update_signal(created["id"], "triaged")

    def test_dispatcher_is_blocked(self, temp_db, monkeypatch):
        created = report_signal("friction", "a")
        monkeypatch.setenv("OW_ROLE", "dispatcher")

        with pytest.raises(CapabilityError):
            update_signal(created["id"], "triaged")

    def test_non_ow_session_can_update(self, temp_db):
        """非owセッション(role未解決)はcheck_capabilityのbackward compatで通過する。"""
        created = report_signal("friction", "a")

        result = update_signal(created["id"], "triaged")

        assert result["signal"]["status"] == "triaged"
