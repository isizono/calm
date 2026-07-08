"""relay_publish（labels routing 配布）の unit test。

publish は relay へ直接 HTTP せず relay_outbox への INSERT で完結する
（配達は server 内の常駐配達ループの責務）。labels 検証と handle 自動付与を検証する。
"""
import json
import sqlite3

import pytest

from src.relay_sdk.outbox import create_outbox_table
from src.services.relay import declarations, service


@pytest.fixture(autouse=True)
def relay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_outbox_table(conn)
    yield conn
    conn.close()


def _publish(conn, labels, body="hello", title=None, session_id="sess-1"):
    return service.publish_with_conn(
        conn, caller_session_id=session_id, labels=labels, body=body, title=title
    )


class TestPublishSuccess:
    def test_inserts_one_outbox_row_with_auto_handle(self, conn):
        result = _publish(conn, ["task:123"])
        assert "error" not in result

        rows = conn.execute("SELECT * FROM relay_outbox").fetchall()
        assert len(rows) == 1
        stored_labels = json.loads(rows[0]["labels"])
        handle = declarations.load("sess-1")["handle"]
        assert f"handle:{handle}" in stored_labels
        assert "task:123" in stored_labels
        assert rows[0]["ref_id"] == "hello"

    def test_returns_outbox_id_and_final_labels(self, conn):
        result = _publish(conn, ["task:123"])
        assert isinstance(result["outbox_id"], int)
        assert f"handle:{result['handle']}" in result["labels"]

    def test_tag_namespace_only_labels_are_accepted(self, conn):
        """cc-memory tag namespace（domain:/intent:）は中核 entity ではないため、これのみでも有効な publish になる。"""
        result = _publish(conn, ["domain:cc-memory", "intent:design"])
        assert "error" not in result

    def test_opaque_prefixes_are_accepted(self, conn):
        """room:/task:/未知 prefix は不透明 label として受理される。"""
        result = _publish(conn, ["room:planning", "task:build", "custom:thing"])
        assert "error" not in result

    def test_existing_own_handle_label_is_not_duplicated(self, conn):
        handle = declarations.ensure("sess-1")["handle"]
        result = _publish(conn, [f"handle:{handle}", "task:1"])
        assert result["labels"].count(f"handle:{handle}") == 1

    def test_title_is_stored(self, conn):
        _publish(conn, ["task:123"], title="見出し")
        row = conn.execute("SELECT title FROM relay_outbox").fetchone()
        assert row["title"] == "見出し"


class TestPublishValidation:
    def test_empty_labels_rejected(self, conn):
        result = _publish(conn, [])
        assert result["error"]["code"] == "validation"

    def test_role_prefix_rejected(self, conn):
        result = _publish(conn, ["role:navigator", "task:1"])
        assert result["error"]["code"] == "validation"
        assert "role:" in result["error"]["message"]

    def test_empty_body_rejected(self, conn):
        result = _publish(conn, ["task:1"], body="")
        assert result["error"]["code"] == "validation"

    def test_non_string_label_rejected(self, conn):
        result = _publish(conn, ["task:1", 42])
        assert result["error"]["code"] == "validation"

    def test_overlong_title_rejected(self, conn):
        result = _publish(conn, ["task:1"], title="x" * 201)
        assert result["error"]["code"] == "validation"

    def test_no_row_inserted_on_validation_error(self, conn):
        _publish(conn, [])
        _publish(conn, ["role:navigator"])
        assert conn.execute("SELECT COUNT(*) FROM relay_outbox").fetchone()[0] == 0


class TestReservedEntityNamespace:
    """cc-memory の予約 namespace（entity:/event:/topic:/activity:/decision:/log:/
    material:/tag:/habit:）は label として使えず拒否される。relay label は実在
    チェックを行わない不透明文字列のため、これらの語彙をそのまま許すと存在しない/
    未検証の entity への関連付けを誤認させる。
    """

    @pytest.mark.parametrize(
        "entity_type",
        ["topic", "activity", "decision", "log", "material", "tag", "habit"],
    )
    def test_core_entity_prefix_rejected(self, conn, entity_type):
        result = _publish(conn, ["task:build", f"{entity_type}:1"])
        assert result["error"]["code"] == "validation"
        assert f"{entity_type}:" in result["error"]["message"]

    @pytest.mark.parametrize("meta_namespace", ["entity", "event"])
    def test_meta_namespace_rejected(self, conn, meta_namespace):
        result = _publish(conn, ["task:build", f"{meta_namespace}:decision"])
        assert result["error"]["code"] == "validation"
        assert f"{meta_namespace}:" in result["error"]["message"]

    def test_core_entity_prefix_blocks_call_even_with_other_valid_labels(self, conn):
        """中核 entity prefix が1つでも含まれれば、他の label が有効でも呼び出し全体を拒否する（部分成功しない）。"""
        result = _publish(conn, ["task:build", "domain:cc-memory", "topic:45"])
        assert result["error"]["code"] == "validation"

    def test_no_row_inserted_when_entity_prefix_rejected(self, conn):
        _publish(conn, ["decision:1"])
        assert conn.execute("SELECT COUNT(*) FROM relay_outbox").fetchone()[0] == 0


class TestLabelLengthCap:
    """1 つの label string は 200 chars 以内（decision 3074 D.1）。"""

    def test_label_within_cap_is_accepted(self, conn):
        result = _publish(conn, ["x" * 200])
        assert "error" not in result

    def test_overlong_label_rejected(self, conn):
        result = _publish(conn, ["x" * 201])
        assert result["error"]["code"] == "validation"


class TestPublishPreconditions:
    def test_missing_token_returns_explicit_error(self, conn, monkeypatch):
        monkeypatch.delenv("RELAY_BEARER_TOKEN")
        result = _publish(conn, ["task:1"])
        assert result["error"]["code"] == "config_missing"
        assert "RELAY_BEARER_TOKEN" in result["error"]["message"]

    def test_unresolved_session_returns_explicit_error(self, conn):
        result = _publish(conn, ["task:1"], session_id=None)
        assert result["error"]["code"] == "session_unresolved"


class TestValidateLabelsCoreMode:
    """check_reserved=False（entity write → relay outbox の core内部 publish 専用）の
    検証モード。予約 namespace を許可しつつ、role: と200字capは維持する。
    """

    @pytest.mark.parametrize(
        "label",
        [
            "entity:decision", "event:created", "event:updated", "event:retracted",
            "topic:1", "activity:1", "decision:1", "log:1", "material:1",
            "tag:1", "habit:1",
        ],
    )
    def test_reserved_namespace_is_accepted(self, label):
        from src.services.relay.service import validate_labels
        assert validate_labels([label], check_reserved=False) is None

    def test_role_prefix_still_rejected(self):
        from src.services.relay.service import validate_labels
        message = validate_labels(["role:navigator"], check_reserved=False)
        assert message is not None
        assert "role:" in message

    def test_label_length_cap_still_enforced(self):
        from src.services.relay.service import validate_labels
        assert validate_labels(["entity:decision"], check_reserved=False) is None
        message = validate_labels(["x" * 201], check_reserved=False)
        assert message is not None
