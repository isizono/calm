"""get_by_id / get_by_ids の decision 分岐で is_superseded / superseded_by が付くことの検証

search() 結果には既に superseded_by が付与されている (test_search_superseded_by.py) が、
get_by_id / get_by_ids には supersede 情報が欠落していた露出漏れの修正を検証する。
"""
import os
import tempfile

import pytest

from src.db import init_database
from src.services.relation_service import add_relation
from src.services.retract_service import retract
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
    t = add_topic(title="get_by_id supersede テスト", description="Desc", tags=DEFAULT_TAGS)
    return t["topic_id"]


def _link_supersede(newer_id: int, older_id: int) -> None:
    result = add_relation(
        "decision",
        newer_id,
        [{"type": "decision", "ids": [older_id]}],
        relation_type="supersedes",
    )
    assert "error" not in result, result


class TestGetByIdSupersedeInfo:
    def test_not_superseded_decision_has_false_and_none(self, topic_id):
        """supersedeされていないdecisionはis_superseded=False, superseded_by=None"""
        d = add_decision(decision="独立した決定", reason="理由", topic_id=topic_id)

        res = get_by_id("decision", d["decision_id"])

        assert "error" not in res
        data = res["data"]
        assert data["is_superseded"] is False
        assert data["superseded_by"] is None

    def test_superseded_decision_has_true_and_source_id(self, topic_id):
        """supersedeされているdecisionはis_superseded=True, superseded_by=最新1hopのsource_id"""
        d_old = add_decision(decision="古い決定", reason="古い理由", topic_id=topic_id)
        d_new = add_decision(decision="新しい決定", reason="新しい理由", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        res = get_by_id("decision", d_old["decision_id"])

        assert "error" not in res
        data = res["data"]
        assert data["is_superseded"] is True
        assert data["superseded_by"] == d_new["decision_id"]

    def test_other_existing_fields_unchanged(self, topic_id):
        """supersede情報追加後もid/title/decision/reason/tags/created_atは従来通り存在する"""
        d = add_decision(decision="決定本文", reason="理由本文", topic_id=topic_id)

        res = get_by_id("decision", d["decision_id"])
        data = res["data"]

        assert data["id_raw"] == d["decision_id"]
        assert data["decision"] == "決定本文"
        assert data["reason"] == "理由本文"
        assert data["tags"] == DEFAULT_TAGS
        assert "created_at" in data

    def test_non_decision_type_has_no_supersede_fields(self, topic_id):
        """decision以外のtypeにはis_superseded/superseded_byは付かない"""
        res = get_by_id("topic", topic_id)

        assert "error" not in res
        data = res["data"]
        assert "is_superseded" not in data
        assert "superseded_by" not in data


class TestGetByIdsSupersedeInfo:
    def test_batch_includes_supersede_info_per_item(self, topic_id):
        """get_by_ids は複数decisionそれぞれにsupersede情報を独立して付与する"""
        d_old = add_decision(decision="古い決定2", reason="古い理由2", topic_id=topic_id)
        d_new = add_decision(decision="新しい決定2", reason="新しい理由2", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        res = get_by_ids(
            [
                {"type": "decision", "id": d_old["decision_id"]},
                {"type": "decision", "id": d_new["decision_id"]},
            ]
        )

        assert "error" not in res
        results = res["results"]
        old_data = results[0]["data"]
        new_data = results[1]["data"]
        assert old_data["is_superseded"] is True
        assert old_data["superseded_by"] == d_new["decision_id"]
        assert new_data["is_superseded"] is False
        assert new_data["superseded_by"] is None

    def test_superseded_decision_not_hidden_from_batch(self, topic_id):
        """superseded済みでも一覧から消えず、注記付きでそのまま返る"""
        d_old = add_decision(decision="消えない古い決定", reason="理由", topic_id=topic_id)
        d_new = add_decision(decision="消えない新しい決定", reason="理由", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        res = get_by_ids([{"type": "decision", "id": d_old["decision_id"]}])

        assert "error" not in res
        assert len(res["results"]) == 1
        assert res["results"][0]["data"]["id_raw"] == d_old["decision_id"]

    def test_retracted_and_superseded_decision_still_shows_supersede_info(self, topic_id):
        """retract済みでもsupersede情報は引き続き付与される（retractとsupersedeは独立した状態）"""
        d_old = add_decision(decision="retract対象", reason="理由", topic_id=topic_id)
        d_new = add_decision(decision="置き換え先", reason="理由", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])
        retract("decision", [d_old["decision_id"]])

        res = get_by_id("decision", d_old["decision_id"])

        assert "error" not in res
        data = res["data"]
        assert data["is_superseded"] is True
        assert data["superseded_by"] == d_new["decision_id"]
        assert data["retracted_at"] is not None
