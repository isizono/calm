"""relay_receive（inbox drain）の federation 由来メッセージマーキングの unit test。

federation 経由（他 peer 由来）のメッセージは受信セッションが指示として実行しては
ならない（prompt injection 対策）。relay 側が刻印する `publisher_identity`
フィールドに '@' を含むレコードだけを federation 由来としてマークし、指示として
実行しないよう促す注意書きを付与する契約を検証する。
"""
import pytest

from src.services.relay import inbox, service


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))


class TestFederationOriginMarking:
    def test_publisher_identity_with_at_sign_is_marked_as_federation(self):
        inbox.append("sess-1", {"body": "hello", "publisher_identity": "sub-abc@friend"})

        result = service.relay_receive(caller_session_id="sess-1")

        assert "error" not in result, result
        message = result["messages"][0]
        assert message["is_federation_origin"] is True
        assert "trust_notice" in message
        assert message["trust_notice"] == service.FEDERATION_TRUST_NOTICE

    def test_publisher_identity_without_at_sign_is_not_marked(self):
        inbox.append("sess-1", {"body": "hello", "publisher_identity": "local-session"})

        result = service.relay_receive(caller_session_id="sess-1")

        message = result["messages"][0]
        assert "is_federation_origin" not in message
        assert "trust_notice" not in message

    def test_missing_publisher_identity_is_not_marked(self):
        """relay 側の publisher_identity 対応が未反映の環境を模す（フィールド自体が無い）。"""
        inbox.append("sess-1", {"body": "hello"})

        result = service.relay_receive(caller_session_id="sess-1")

        message = result["messages"][0]
        assert "is_federation_origin" not in message
        assert "trust_notice" not in message

    def test_mixed_batch_marks_only_federation_records(self):
        inbox.append("sess-1", {"body": "local", "publisher_identity": "local-session"})
        inbox.append("sess-1", {"body": "remote", "publisher_identity": "sub-xyz@friend"})

        result = service.relay_receive(caller_session_id="sess-1")

        local_message, remote_message = result["messages"]
        assert "is_federation_origin" not in local_message
        assert remote_message["is_federation_origin"] is True

    def test_non_string_publisher_identity_is_not_marked(self):
        """型不正（relay 側の不具合等）でも local 由来扱いに倒し、マーク付与でクラッシュしない。"""
        inbox.append("sess-1", {"body": "hello", "publisher_identity": 123})

        result = service.relay_receive(caller_session_id="sess-1")

        message = result["messages"][0]
        assert "is_federation_origin" not in message
