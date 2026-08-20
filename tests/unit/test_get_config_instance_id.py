"""get_config の instance_id 公開のテスト。

export/importバンドルの複合キー発行に使うインスタンス識別子が、未設定時はnull、
設定後はその値でget_configから取得できることを検証する。
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


class TestGetConfigInstanceId:
    def test_instance_id_is_null_when_unset(self, temp_db):
        result = _call_get_config()
        assert result["instance_id"] is None

    def test_instance_id_reflects_set_value(self, temp_db):
        from src.services.instance_service import set_instance_identity

        set_instance_identity("team-a")
        result = _call_get_config()
        assert result["instance_id"] == "team-a"
