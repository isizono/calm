"""get_config の budget_defaults 公開のテスト。

budget_service が把握する予算関連の既定値一覧が get_config から取得できることを
検証する。値そのものの正しさ（src.configとの一致）は test_budget_service.py 側で
担保する。
"""
def _call_get_config():
    from src.main import get_config

    return get_config()


class TestGetConfigBudgetDefaults:
    def test_budget_defaults_key_present(self):
        result = _call_get_config()
        assert "budget_defaults" in result

    def test_budget_defaults_matches_budget_service(self):
        from src.services import budget_service

        result = _call_get_config()
        assert result["budget_defaults"] == budget_service.BUDGET_DEFAULTS

    def test_existing_fields_unaffected(self):
        result = _call_get_config()
        assert "precedent_budget_chars" in result
        assert "read_tool_limits" in result
