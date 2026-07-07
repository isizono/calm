"""relay 4 動詞 tool の MCP 登録・docstring 契約のテスト。"""
from tests.helpers import all_tool_descriptions as _all_tool_descriptions

RELAY_TOOLS = ("relay_post", "relay_publish", "relay_subscribe", "relay_receive")


class TestToolRegistration:
    def test_all_relay_tools_are_exposed_via_mcp(self):
        descriptions = _all_tool_descriptions()
        for name in RELAY_TOOLS:
            assert name in descriptions, f"{name} が MCP tool として未登録"


class TestReceiveDocstringContract:
    def test_receive_mentions_at_least_once_duplication(self):
        """受信契約（at-least-once・重複到達がありうる）が description に明記されている。"""
        desc = _all_tool_descriptions()["relay_receive"]
        assert "at-least-once" in desc
        assert "重複" in desc


class TestPostDocstringContract:
    def test_post_mentions_single_identity_scope(self):
        """自 server 名義の stream のみ扱う制約が description に明記されている。"""
        desc = _all_tool_descriptions()["relay_post"]
        assert "自 server 名義" in desc


class TestPublishDocstringContract:
    def test_publish_mentions_entity_namespace_reservation(self):
        """cc-memory の中核 entity namespace は label として使えずエラーになる契約が明記されている。"""
        desc = _all_tool_descriptions()["relay_publish"]
        assert "entity" in desc
        assert "エラー" in desc


class TestSubscribeReceivePairing:
    """relay_subscribe と relay_receive が購読宣言/受信で役割分担していることの相互参照。"""

    def test_subscribe_and_receive_reference_each_other(self):
        descriptions = _all_tool_descriptions()
        assert "relay_receive" in descriptions["relay_subscribe"]
        assert "relay_subscribe" in descriptions["relay_receive"]
