"""check_inへの全体予算適用と、それを支える契約(checkin_scope・pinnedへのflavor
適用・add_activity経路の最終化)の統合テスト。

本番DBには一切触れない。「本番DBの複製で43,000字のpinを実測した」検証(仕様の
Verification V2)は、合成データ(このテストで作るpin)で代替する。
"""
import pytest

from src.db import get_connection
from src.services.activity_service import add_activity
from src.services.checkin_queries import checkin_scope
from src.services.material_service import add_material
from src.services.pin_service import add_pin
from src.services.relation_service import add_relation
from src.services.topic_service import add_topic
from src.main import check_in as tool_check_in
from src.main import add_activity as tool_add_activity
from src.main import get_material as tool_get_material
from tests.helpers import add_log

DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def activity_id(temp_db):
    return add_activity(
        title="Budget Test Activity", description="for budget tests",
        tags=DEFAULT_TAGS, check_in=False,
    )["activity_id"]


class TestCheckinScope:
    def test_returns_activity_id_and_topic_ids(self, temp_db):
        topic = add_topic(title="T", description="d", tags=DEFAULT_TAGS)
        tid = topic["topic_id"]
        act = add_activity(
            title="A", description="d", tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [tid]}], check_in=False,
        )
        aid = act["activity_id"]
        result = tool_check_in(aid)
        scope = checkin_scope(result)
        assert scope == (aid, [tid])

    def test_returns_none_for_error_response(self, temp_db):
        assert checkin_scope({"error": {"code": "NOT_FOUND", "message": "x"}}) is None

    def test_returns_none_for_non_dict(self, temp_db):
        assert checkin_scope(None) is None
        assert checkin_scope("not a dict") is None

    def test_falsification_missing_activity_key_returns_none(self, temp_db):
        """activityキー自体が無い応答（形が壊れたケース）ではNoneを返す。"""
        assert checkin_scope({"related_topics": [{"id_raw": 1}]}) is None


class TestPinnedFlavorApplied(object):
    """旧main.pyの欠落（pinnedにflavor未適用）を埋めたことの回帰テスト。"""

    def test_check_in_expands_citation_in_pinned_material_content(self, temp_db, activity_id):
        target = add_material(
            title="target", content="body", tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        target_id = target["material_id"]
        owner = add_material(
            title="owner", content=f"see {{{{cite:M#{target_id}}}}} please",
            tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        owner_id = owner["material_id"]
        add_pin("activity", activity_id, "material", owner_id)

        result = tool_check_in(activity_id)  # flavor既定=internal
        pinned_materials = result["anchor"]["pinned"]["materials"]
        assert len(pinned_materials) == 1
        assert f"(M#{target_id})" in pinned_materials[0]["content"]

    def test_check_in_raw_flavor_leaves_pinned_citation_untouched(self, temp_db, activity_id):
        target = add_material(
            title="target", content="body", tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        target_id = target["material_id"]
        owner = add_material(
            title="owner", content=f"see {{{{cite:M#{target_id}}}}} please",
            tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        owner_id = owner["material_id"]
        add_pin("activity", activity_id, "material", owner_id)

        result = tool_check_in(activity_id, flavor="raw")
        pinned_materials = result["anchor"]["pinned"]["materials"]
        assert f"{{{{cite:M#{target_id}}}}}" in pinned_materials[0]["content"]


class TestContextFlavorApplied:
    """flavorがanchor.pinned以外の枠（context.latest_log）にも届くことの回帰テスト。

    _apply_flavor_to_check_in_resultはtier形の全セクションを回るよう書き直した
    ため、pinned以外の枠（context/catalog）で1箇所だけ実地確認する。
    """

    def test_check_in_expands_citation_in_context_latest_log(self, temp_db, activity_id):
        topic = add_topic(title="T", description="d", tags=DEFAULT_TAGS)
        topic_id = topic["topic_id"]
        add_relation("activity", activity_id, [{"type": "topic", "ids": [topic_id]}])
        target = add_material(
            title="target", content="body", tags=DEFAULT_TAGS, source="t",
        )
        target_id = target["material_id"]
        add_log(topic_id, content=f"議事メモ: see {{{{cite:M#{target_id}}}}} for context")

        result = tool_check_in(activity_id)  # flavor既定=internal
        latest_log = result["context"]["latest_log"]
        assert f"(M#{target_id})" in latest_log["content"]


class TestBudgetAppliedToRealCheckIn:
    def test_large_pin_gets_truncated_small_pin_survives_whole(self, temp_db, activity_id):
        """43,000字のpinを合成データで再現する（本番DBの複製の代わり）。
        小さいpin（インデックス相当）は丸ごと残り、大きい方は先頭を残して
        切られ、続きへのポインタで元の本文を取り直せることを確認する。
        """
        index_content = "idx " * 100  # 400字、インデックス相当の小さいpin
        big_content = "x" * 43_000
        index_mat = add_material(
            title="index", content=index_content, tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        big_mat = add_material(
            title="legacy", content=big_content, tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        add_pin("activity", activity_id, "material", index_mat)
        add_pin("activity", activity_id, "material", big_mat)

        result = tool_check_in(activity_id)

        assert "truncated" in result
        # pinnedを枠(3,000字)まで縮めるだけで全体は十分予算内に収まる
        # （before=切り詰め前は予算超過、after=切り詰め後は予算内に収まる）
        assert result["truncated"]["before"] > result["truncated"]["budget"]
        assert result["truncated"]["after"] <= result["truncated"]["budget"]
        assert result["truncated"]["over_budget"] is False

        pinned_materials = {m["id_raw"]: m for m in result["anchor"]["pinned"]["materials"]}
        index_item = pinned_materials[index_mat]
        big_item = pinned_materials[big_mat]

        # 小さい方(インデックス相当)は丸ごと残る
        assert index_item["content"] == index_content
        assert "content_truncated" not in index_item

        # 大きい方は先頭を残して切られ、ポインタが付く
        assert big_item.get("content_truncated") is True
        assert big_item["content"] == big_content[: len(big_item["content"])]
        assert len(big_item["content"]) < len(big_content)
        assert big_item["next"] == [{"tool": "get_material", "args": {"material_id": big_mat}}]

        # ポインタを実際に辿ると元の本文（43,000字）が取れる
        refetched = tool_get_material(big_mat)
        assert refetched["content"] == big_content

    def test_falsification_without_second_material_no_truncation_needed(self, temp_db, activity_id):
        """比較対照: pinが無ければtruncatedは付かない（上のテストがpin由来の
        超過を検出していることの裏取り）。"""
        result = tool_check_in(activity_id)
        assert "truncated" not in result

    def test_raw_flavor_still_applies_budget(self, temp_db, activity_id):
        big_content = "x" * 43_000
        big_mat = add_material(
            title="legacy", content=big_content, tags=DEFAULT_TAGS, source="t",
            related=[{"type": "activity", "ids": [activity_id]}],
        )["material_id"]
        add_pin("activity", activity_id, "material", big_mat)

        result = tool_check_in(activity_id, flavor="raw")
        assert "truncated" in result
        item = result["anchor"]["pinned"]["materials"][0]
        assert len(item["content"]) < len(big_content)


class TestAddActivityFinalization:
    def test_check_in_result_gets_flavor_and_budget_applied(self, temp_db):
        target = add_material(
            title="target", content="body", tags=DEFAULT_TAGS, source="t",
        )
        target_id = target["material_id"]
        owner = add_material(
            title="owner", content=f"see {{{{cite:M#{target_id}}}}}",
            tags=DEFAULT_TAGS, source="t",
        )
        owner_id = owner["material_id"]

        result = tool_add_activity(
            title="New Activity One", description="d", tags=DEFAULT_TAGS,
            pins=[{"type": "material", "ref": owner_id}],
        )
        check_in_result = result["check_in_result"]
        # 旧実装ではpinnedにflavorが未適用のまま返っていた
        pinned_materials = check_in_result["anchor"]["pinned"]["materials"]
        assert f"(M#{target_id})" in pinned_materials[0]["content"]

    def test_check_in_result_gets_budget_applied_when_pin_is_huge(self, temp_db):
        big_content = "x" * 43_000
        big_mat = add_material(
            title="legacy", content=big_content, tags=DEFAULT_TAGS, source="t",
        )["material_id"]

        result = tool_add_activity(
            title="New Activity Legacy Pin", description="d", tags=DEFAULT_TAGS,
            pins=[{"type": "material", "ref": big_mat}],
        )
        check_in_result = result["check_in_result"]
        assert "truncated" in check_in_result
        item = check_in_result["anchor"]["pinned"]["materials"][0]
        assert len(item["content"]) < len(big_content)

    def test_falsification_check_in_false_skips_finalization(self, temp_db):
        """check_in=Falseの経路はcheck_in_result自体を持たない
        （finalizeが余計な副作用を持ち込んでいないことの確認）。"""
        result = tool_add_activity(
            title="No Checkin Activity", description="d", tags=DEFAULT_TAGS, check_in=False,
        )
        assert "check_in_result" not in result
