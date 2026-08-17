"""get_config の budget_defaults 公開のテスト。

budget_service が把握する予算関連の既定値一覧が get_config から取得できることを
検証する。値そのものの正しさ（src.configとの一致）は test_budget_service.py 側で
担保する。

get_configはinstance_id参照のためDBアクセスを行うため、temp_db fixtureが必要。
"""
import os
import tempfile

import pytest

from src.db import init_database


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _call_get_config():
    from src.main import get_config

    return get_config()


class TestGetConfigBudgetDefaults:
    def test_budget_defaults_key_present(self, temp_db):
        result = _call_get_config()
        assert "budget_defaults" in result

    def test_budget_defaults_matches_budget_service(self, temp_db):
        from src.services import budget_service

        result = _call_get_config()
        assert result["budget_defaults"] == budget_service.BUDGET_DEFAULTS

    def test_existing_fields_unaffected(self, temp_db):
        result = _call_get_config()
        assert "precedent_budget_chars" in result
        assert "read_tool_limits" in result

    def test_budget_defaults_includes_response_chars_max(self, temp_db):
        from src.config import PRECEDENT_RESPONSE_CHARS_MAX

        result = _call_get_config()
        assert result["budget_defaults"]["precedent_response_chars_max"] == PRECEDENT_RESPONSE_CHARS_MAX
