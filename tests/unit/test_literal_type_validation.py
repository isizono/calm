"""MCPツール引数の Literal 型バリデーションのテスト。

src/main.py の entity_type / source_type / target_type / entity_types
引数が Literal[...] になっていることで、

- スキーマ JSON で enum が伝播していること
- 許容値以外を渡したとき pydantic ValidationError で弾かれること
- 許容値を渡したときは型検証を通過し、ツール本体が実行されること

を確認する。
"""
import asyncio
import json

import pytest
from pydantic import ValidationError

from src.main import mcp
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic
from tests.helpers import add_decision, all_tool_schemas


DEFAULT_TAGS = ["domain:test"]


def _schema_for(name: str) -> dict:
    """指定ツールの入力スキーマ（properties）を取り出す。"""
    schemas = all_tool_schemas()
    if name not in schemas:
        raise AssertionError(f"tool not found: {name}")
    return schemas[name]


def _enum_of(schema: dict, prop: str) -> list[str]:
    """properties[prop] の enum を抽出する（anyOf/items 経由含む）。"""
    p = schema["properties"][prop]
    if "enum" in p:
        return list(p["enum"])
    if "anyOf" in p:
        for branch in p["anyOf"]:
            if "enum" in branch:
                return list(branch["enum"])
            if branch.get("type") == "array" and "items" in branch:
                items = branch["items"]
                if "enum" in items:
                    return list(items["enum"])
    if p.get("type") == "array" and "items" in p:
        items = p["items"]
        if "enum" in items:
            return list(items["enum"])
    raise AssertionError(
        f"enum not found in property {prop}: {json.dumps(p)}"
    )


class TestSchemaEnumPropagation:
    """各ツールの入力スキーマで Literal が enum として出力されること。"""

    def test_get_logs_entity_type_enum(self):
        schema = _schema_for("get_logs")
        assert sorted(_enum_of(schema, "entity_type")) == sorted(["topic", "activity"])

    def test_get_decisions_entity_type_enum(self):
        schema = _schema_for("get_decisions")
        assert sorted(_enum_of(schema, "entity_type")) == sorted(["topic", "activity"])

    def test_search_entity_type_enum(self):
        schema = _schema_for("search")
        assert sorted(_enum_of(schema, "entity_type")) == sorted(
            ["topic", "decision", "activity", "log", "material"]
        )

    def test_add_relation_source_type_enum(self):
        schema = _schema_for("add_relation")
        assert sorted(_enum_of(schema, "source_type")) == sorted(
            ["topic", "activity", "material", "decision", "log"]
        )

    def test_remove_relation_source_type_enum(self):
        schema = _schema_for("remove_relation")
        assert sorted(_enum_of(schema, "source_type")) == sorted(
            ["topic", "activity", "material", "decision", "log"]
        )

    def test_get_map_entity_type_enum(self):
        schema = _schema_for("get_map")
        assert sorted(_enum_of(schema, "entity_type")) == sorted(
            ["topic", "activity", "material", "decision", "log"]
        )

    def test_add_pin_source_target_enum(self):
        schema = _schema_for("add_pin")
        expected = sorted(["tag", "activity", "topic", "decision", "log", "material"])
        assert sorted(_enum_of(schema, "source_type")) == expected
        assert sorted(_enum_of(schema, "target_type")) == expected

    def test_remove_pin_source_target_enum(self):
        schema = _schema_for("remove_pin")
        expected = sorted(["tag", "activity", "topic", "decision", "log", "material"])
        assert sorted(_enum_of(schema, "source_type")) == expected
        assert sorted(_enum_of(schema, "target_type")) == expected

    def test_retract_entity_type_enum(self):
        schema = _schema_for("retract")
        assert sorted(_enum_of(schema, "entity_type")) == sorted(
            ["decision", "log", "material"]
        )

    def test_get_timeline_entity_types_enum(self):
        schema = _schema_for("get_timeline")
        assert sorted(_enum_of(schema, "entity_types")) == sorted(
            ["decision", "log", "material"]
        )


# --- 不正値での ValidationError 検証 -------------------------------------

INVALID_CASES = [
    # (tool_name, args, typo を含むキー)
    ("get_logs", {"entity_type": "decisin", "entity_id": 1}, "entity_type"),
    ("get_logs", {"entity_type": "log", "entity_id": 1}, "entity_type"),  # 許容外（topic/activityのみ）
    ("get_decisions", {"entity_type": "decisin", "entity_id": 1}, "entity_type"),
    ("search", {"keyword": "foo", "entity_type": "decisin"}, "entity_type"),
    (
        "add_relation",
        {"source_type": "topik", "source_id": 1, "targets": []},
        "source_type",
    ),
    (
        "remove_relation",
        {"source_type": "topik", "source_id": 1, "targets": []},
        "source_type",
    ),
    ("get_map", {"entity_type": "tag", "entity_id": 1}, "entity_type"),  # tag は不可
    (
        "add_pin",
        {"source_type": "decisin", "source_ref": 1, "target_type": "log", "target_ref": 2},
        "source_type",
    ),
    (
        "add_pin",
        {"source_type": "log", "source_ref": 1, "target_type": "decisin", "target_ref": 2},
        "target_type",
    ),
    (
        "remove_pin",
        {"source_type": "logg", "source_ref": 1, "target_type": "tag", "target_ref": 2},
        "source_type",
    ),
    ("retract", {"entity_type": "decisin", "ids": [1]}, "entity_type"),
    ("retract", {"entity_type": "topic", "ids": [1]}, "entity_type"),
    ("get_timeline", {"entity_types": ["decision", "topik"]}, "entity_types"),
]


@pytest.mark.parametrize("tool_name,args,target_key", INVALID_CASES)
def test_invalid_literal_value_raises_validation_error(tool_name, args, target_key):
    """許容値以外の文字列を渡すと ValidationError で弾かれる。"""

    async def _call():
        return await mcp.call_tool(tool_name, args)

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(_call())

    msg = str(excinfo.value)
    assert "Input should be" in msg
    assert target_key in msg


# --- 正常値で従来通り動作 -------------------------------------------------

class TestValidLiteralValuePassesThrough:
    """許容値を渡したときは型検証を通過し、ツール本体が実行されること。"""

    def test_get_logs_valid_topic(self, temp_db):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)

        async def _call():
            return await mcp.call_tool(
                "get_logs", {"entity_type": "topic", "entity_id": topic["topic_id"]}
            )

        result = asyncio.run(_call())
        payload = result.structured_content or result.content
        # ツール本体が呼ばれた=ValidationErrorが出ていない。logs/error いずれかのキーが含まれる。
        assert isinstance(payload, dict)

    def test_get_decisions_valid_activity(self, temp_db):
        act = add_activity(
            title="a", description="d", tags=["intent:discuss", "domain:test"]
        )

        async def _call():
            return await mcp.call_tool(
                "get_decisions",
                {"entity_type": "activity", "entity_id": act["activity_id"]},
            )

        result = asyncio.run(_call())
        payload = result.structured_content or result.content
        assert isinstance(payload, dict)

    def test_search_valid_entity_type(self, temp_db, disable_embedding):
        async def _call():
            return await mcp.call_tool(
                "search", {"keyword": "xx", "entity_type": "topic"}
            )

        result = asyncio.run(_call())
        payload = result.structured_content or result.content
        assert isinstance(payload, dict)

    def test_retract_valid_decision(self, temp_db):
        """実 decision を作って valid な ids でツール本体が成功実行されることを確認する。"""
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        dec = add_decision(
            decision="d",
            reason="r",
            topic_id=topic["topic_id"],
            tags=DEFAULT_TAGS,
        )
        decision_id = dec["decision_id"]

        async def _call():
            return await mcp.call_tool(
                "retract", {"entity_type": "decision", "ids": [decision_id]}
            )

        result = asyncio.run(_call())
        payload = result.structured_content or result.content
        # service 層が成功時に返す形（success リストに対象 id を含む）であること
        assert isinstance(payload, dict)
        assert "error" not in payload
        assert decision_id in payload.get("success", [])

    def test_get_timeline_valid_subset(self, temp_db, disable_embedding):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)

        async def _call():
            return await mcp.call_tool(
                "get_timeline",
                {"topic_id": topic["topic_id"], "entity_types": ["decision", "log"]},
            )

        result = asyncio.run(_call())
        payload = result.structured_content or result.content
        assert isinstance(payload, dict)
