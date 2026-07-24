"""get_by_id / get_by_ids の decision 分岐で destabilization が付くことの検証

test_get_by_id_supersede.py の supersede 版に対応する destabilizes 版。
compute_destabilization_info_batch がバッチ経路・単独経路（destabilization_map未指定）
の両方で正しく機能し、N+1 を起こさないことを検証する。
"""
import os
import tempfile

import pytest

from src.db import init_database
from src.services.destabilization_service import resolve_destabilization
from src.services.relation_service import add_relation
from src.services.search_service import get_by_id, get_by_ids
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic_id(temp_db):
    t = add_topic(title="get_by_id destabilization テスト", description="Desc", tags=DEFAULT_TAGS)
    return t["topic_id"]


def _link_destabilizes(source_id: int, target_id: int) -> None:
    result = add_relation(
        "decision",
        source_id,
        [{"type": "decision", "ids": [target_id]}],
        relation_type="destabilizes",
    )
    assert "error" not in result, result


class TestGetByIdDestabilizationInfo:
    """get_by_id 単独呼び出し（destabilization_map 未指定、対象1件だけを算出する経路）"""

    def test_no_destabilizes_edge_omits_key(self, topic_id):
        """destabilizesエッジが無いdecisionはdestabilizationキーが付かない"""
        d = add_decision(decision="独立した決定", reason="理由", topic_id=topic_id)

        res = get_by_id("decision", d["decision_id"])

        assert "error" not in res
        assert "destabilization" not in res["data"]

    def test_unresolved_destabilizes_edge_adds_key(self, topic_id):
        """未resolveなdestabilizesエッジがあればdestabilizationキーが付く"""
        source = add_decision(decision="軸変更", reason="軸変更理由", topic_id=topic_id)
        target = add_decision(decision="影響先", reason="理由", topic_id=topic_id)
        _link_destabilizes(source["decision_id"], target["decision_id"])

        res = get_by_id("decision", target["decision_id"])

        assert "error" not in res
        data = res["data"]
        assert data["destabilization"]["unresolved_count"] == 1
        assert data["destabilization"]["destabilized_by"] == [source["decision_id"]]
        assert data["destabilization"]["sources"][0]["kind_reason"] == "軸変更理由"

    def test_resolved_destabilizes_edge_removes_key(self, topic_id):
        """resolve済みのdestabilizesエッジはdestabilizationキーを付けない"""
        source = add_decision(decision="軸変更", reason="理由", topic_id=topic_id)
        target = add_decision(decision="影響先", reason="理由", topic_id=topic_id)
        _link_destabilizes(source["decision_id"], target["decision_id"])
        resolve_destabilization(source["decision_id"], target["decision_id"], "reaffirmed")

        res = get_by_id("decision", target["decision_id"])

        assert "error" not in res
        assert "destabilization" not in res["data"]

    def test_non_decision_type_has_no_destabilization_field(self, topic_id):
        """decision以外のtypeにはdestabilizationは付かない"""
        res = get_by_id("topic", topic_id)

        assert "error" not in res
        assert "destabilization" not in res["data"]


class TestGetByIdsDestabilizationInfo:
    """get_by_ids バッチ呼び出し（destabilization_map 事前算出経路）"""

    def test_batch_includes_destabilization_per_item(self, topic_id):
        """get_by_ids は複数decisionそれぞれにdestabilization情報を独立して付与する"""
        source = add_decision(decision="軸変更B", reason="理由B", topic_id=topic_id)
        target = add_decision(decision="影響先B", reason="理由", topic_id=topic_id)
        untouched = add_decision(decision="無関係B", reason="理由", topic_id=topic_id)
        _link_destabilizes(source["decision_id"], target["decision_id"])

        res = get_by_ids(
            [
                {"type": "decision", "id": target["decision_id"]},
                {"type": "decision", "id": untouched["decision_id"]},
            ]
        )

        assert "error" not in res
        results = res["results"]
        assert results[0]["data"]["destabilization"]["unresolved_count"] == 1
        assert "destabilization" not in results[1]["data"]

    def test_multiple_decisions_issue_single_destabilization_query(self, topic_id, monkeypatch):
        """decisionが複数件でも compute_destabilization_info_batch は1回だけ呼ばれる（N+1回避）"""
        import src.services.search_service as search_service

        source = add_decision(decision="軸変更C", reason="理由C", topic_id=topic_id)
        target = add_decision(decision="影響先C", reason="理由", topic_id=topic_id)
        indep = add_decision(decision="独立C", reason="理由", topic_id=topic_id)
        _link_destabilizes(source["decision_id"], target["decision_id"])

        real = search_service.compute_destabilization_info_batch
        calls: list[list[int]] = []

        def _spy(conn, decision_ids):
            calls.append(list(decision_ids))
            return real(conn, decision_ids)

        monkeypatch.setattr(search_service, "compute_destabilization_info_batch", _spy)

        res = get_by_ids(
            [
                {"type": "decision", "id": target["decision_id"]},
                {"type": "decision", "id": indep["decision_id"]},
                {"type": "decision", "id": source["decision_id"]},
            ]
        )

        assert "error" not in res
        assert len(calls) == 1
        assert set(calls[0]) == {
            target["decision_id"],
            indep["decision_id"],
            source["decision_id"],
        }
