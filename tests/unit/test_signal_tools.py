"""report_signal / get_signals / update_signal MCPツールのユニットテスト

src.main 経由の統合的な挙動 (validationエラーのdict化) を検証する。
record_signal/get_signals/update_signalの詳細な分岐は
tests/unit/test_signal_service.py が担う。
"""
from src.main import report_signal, get_signals, update_signal


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


class TestUpdateSignalTool:
    def test_can_update(self, temp_db):
        created = report_signal("friction", "a")

        result = update_signal(created["id"], "triaged")

        assert result["signal"]["status"] == "triaged"
