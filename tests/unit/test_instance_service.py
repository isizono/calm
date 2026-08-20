"""instance_serviceの単体テスト

instance_idのバリデーション・初回設定・不変性(force無し変更拒否)・
force指定での上書きを検証する。
"""
from src.db import get_connection
from src.services.instance_service import (
    get_instance_id,
    get_instance_id_with_conn,
    set_instance_identity,
)


class TestValidation:
    def test_rejects_uppercase(self, temp_db):
        result = set_instance_identity("Team-A")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_rejects_leading_digit(self, temp_db):
        result = set_instance_identity("1team")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_rejects_underscore(self, temp_db):
        result = set_instance_identity("team_a")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_rejects_too_short(self, temp_db):
        result = set_instance_identity("ab")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_rejects_too_long(self, temp_db):
        result = set_instance_identity("a" * 33)
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_rejects_empty(self, temp_db):
        result = set_instance_identity("")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_accepts_minimum_length(self, temp_db):
        result = set_instance_identity("abc")
        assert "error" not in result

    def test_accepts_hyphens_and_digits(self, temp_db):
        result = set_instance_identity("team-a2")
        assert "error" not in result


class TestFirstTimeSet:
    def test_returns_instance_id_and_created_at(self, temp_db):
        result = set_instance_identity("team-a")
        assert "error" not in result
        assert result["instance_id"] == "team-a"
        assert result["created_at"] is not None

    def test_persists_to_db(self, temp_db):
        set_instance_identity("team-a")
        conn = get_connection()
        try:
            assert get_instance_id_with_conn(conn) == "team-a"
        finally:
            conn.close()

    def test_get_instance_id_reflects_set_value(self, temp_db):
        assert get_instance_id() is None
        set_instance_identity("team-a")
        assert get_instance_id() == "team-a"


class TestImmutability:
    def test_second_set_without_force_is_rejected(self, temp_db):
        set_instance_identity("team-a")
        result = set_instance_identity("team-b")
        assert result["error"]["code"] == "ALREADY_EXISTS"
        assert "team-a" in result["error"]["message"]

    def test_rejected_set_does_not_change_stored_value(self, temp_db):
        set_instance_identity("team-a")
        set_instance_identity("team-b")
        assert get_instance_id() == "team-a"

    def test_second_set_with_force_overwrites(self, temp_db):
        set_instance_identity("team-a")
        result = set_instance_identity("team-b", force=True)
        assert "error" not in result
        assert result["instance_id"] == "team-b"
        assert get_instance_id() == "team-b"
