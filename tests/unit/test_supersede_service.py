"""supersede_service (chain 計算 + superseded_by マップ) の単体テスト"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.supersede_service import (
    _bfs_related,
    compute_supersede_info,
    compute_supersede_info_batch,
    get_superseded_by_batch,
)
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
    t = add_topic(title="テスト", description="Desc", tags=DEFAULT_TAGS)
    return t["topic_id"]


def _link_supersede(newer_id: int, older_id: int) -> None:
    """newer が older を supersede するリレーションを張る。"""
    result = add_relation(
        "decision",
        newer_id,
        [{"type": "decision", "ids": [older_id]}],
        relation_type="supersedes",
    )
    assert "error" not in result, result


class TestBfsRelated:
    """_bfs_related の方向別テスト"""

    def test_no_edges_returns_empty(self, topic_id):
        """supersede 関係が無い decision は BFS 結果が空"""
        d = add_decision(decision="d1", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            assert _bfs_related(conn, d["decision_id"], direction="older") == set()
            assert _bfs_related(conn, d["decision_id"], direction="newer") == set()
        finally:
            conn.close()

    def test_older_direction_follows_target(self, topic_id):
        """direction=older は source_id=x → target_id を辿る"""
        d_old = add_decision(decision="old", reason="r", topic_id=topic_id)
        d_mid = add_decision(decision="mid", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="new", reason="r", topic_id=topic_id)
        _link_supersede(d_mid["decision_id"], d_old["decision_id"])
        _link_supersede(d_new["decision_id"], d_mid["decision_id"])

        conn = get_connection()
        try:
            older = _bfs_related(conn, d_new["decision_id"], direction="older")
        finally:
            conn.close()
        assert older == {d_mid["decision_id"], d_old["decision_id"]}

    def test_newer_direction_follows_source(self, topic_id):
        """direction=newer は target_id=x → source_id を辿る"""
        d_old = add_decision(decision="old", reason="r", topic_id=topic_id)
        d_mid = add_decision(decision="mid", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="new", reason="r", topic_id=topic_id)
        _link_supersede(d_mid["decision_id"], d_old["decision_id"])
        _link_supersede(d_new["decision_id"], d_mid["decision_id"])

        conn = get_connection()
        try:
            newer = _bfs_related(conn, d_old["decision_id"], direction="newer")
        finally:
            conn.close()
        assert newer == {d_mid["decision_id"], d_new["decision_id"]}

    def test_invalid_direction_raises(self, topic_id):
        """不正な direction は ValueError"""
        d = add_decision(decision="d1", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            with pytest.raises(ValueError):
                _bfs_related(conn, d["decision_id"], direction="sideways")
        finally:
            conn.close()


class TestComputeSupersedeInfo:
    """compute_supersede_info の単一 decision 挙動"""

    def test_solo_decision_returns_self_only(self, topic_id):
        """supersede 関係が無い decision は chain が自身1件、is_superseded=False"""
        d = add_decision(decision="独立", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            info = compute_supersede_info(conn, d["decision_id"])
        finally:
            conn.close()
        assert info == {
            "is_superseded": False,
            "supersede_chain": [d["decision_id"]],
        }

    def test_linear_chain_ordered_old_to_new(self, topic_id):
        """直線 chain (d_old → d_mid → d_new) は中央から見て古い→新しい順"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_mid = add_decision(decision="中", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_mid["decision_id"], d_old["decision_id"])
        _link_supersede(d_new["decision_id"], d_mid["decision_id"])

        conn = get_connection()
        try:
            info = compute_supersede_info(conn, d_mid["decision_id"])
        finally:
            conn.close()

        assert info["is_superseded"] is True
        assert info["supersede_chain"] == [
            d_old["decision_id"],
            d_mid["decision_id"],
            d_new["decision_id"],
        ]

    def test_head_of_chain_not_superseded(self, topic_id):
        """chain の先頭 (最新 decision) は is_superseded=False で chain 全件返す"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            info = compute_supersede_info(conn, d_new["decision_id"])
        finally:
            conn.close()

        assert info["is_superseded"] is False
        assert info["supersede_chain"] == [
            d_old["decision_id"],
            d_new["decision_id"],
        ]

    def test_branching_chain_dedupes_ids(self, topic_id):
        """d_old が d_a と d_b の両方に supersede される分岐 chain は重複なく chain 化される"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_a = add_decision(decision="A", reason="r", topic_id=topic_id)
        d_b = add_decision(decision="B", reason="r", topic_id=topic_id)
        _link_supersede(d_a["decision_id"], d_old["decision_id"])
        _link_supersede(d_b["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            info = compute_supersede_info(conn, d_old["decision_id"])
        finally:
            conn.close()

        assert info["is_superseded"] is True
        chain = info["supersede_chain"]
        assert set(chain) == {
            d_old["decision_id"],
            d_a["decision_id"],
            d_b["decision_id"],
        }
        # 重複なし
        assert len(chain) == len(set(chain))
        # created_at 昇順で d_old が先頭
        assert chain[0] == d_old["decision_id"]

    def test_retracted_decision_still_in_chain(self, topic_id):
        """retract 済み decision も supersede chain に残す"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])
        retract("decision", [d_old["decision_id"]])

        conn = get_connection()
        try:
            info = compute_supersede_info(conn, d_new["decision_id"])
        finally:
            conn.close()

        assert d_old["decision_id"] in info["supersede_chain"]


class TestComputeSupersedeInfoBatch:
    """compute_supersede_info_batch のバッチ挙動"""

    def test_empty_input_returns_empty_map(self, temp_db):
        conn = get_connection()
        try:
            assert compute_supersede_info_batch(conn, []) == {}
        finally:
            conn.close()

    def test_batch_returns_per_decision_info(self, topic_id):
        """複数 decision に対して個別の chain 情報を返す"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        d_solo = add_decision(decision="独立", reason="r", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            result = compute_supersede_info_batch(
                conn,
                [d_old["decision_id"], d_new["decision_id"], d_solo["decision_id"]],
            )
        finally:
            conn.close()

        assert result[d_old["decision_id"]]["is_superseded"] is True
        assert result[d_new["decision_id"]]["is_superseded"] is False
        assert result[d_solo["decision_id"]] == {
            "is_superseded": False,
            "supersede_chain": [d_solo["decision_id"]],
        }


class TestGetSupersededByBatch:
    """get_superseded_by_batch: 最新 superseder id を返す"""

    def test_empty_input_returns_empty_map(self, temp_db):
        conn = get_connection()
        try:
            assert get_superseded_by_batch(conn, []) == {}
        finally:
            conn.close()

    def test_not_superseded_returns_none(self, topic_id):
        """supersede されていない decision は None"""
        d = add_decision(decision="d1", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            result = get_superseded_by_batch(conn, [d["decision_id"]])
        finally:
            conn.close()
        assert result == {d["decision_id"]: None}

    def test_superseded_returns_source_id(self, topic_id):
        """supersede されている decision は source_id (最新 superseder) を返す"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            result = get_superseded_by_batch(
                conn, [d_old["decision_id"], d_new["decision_id"]]
            )
        finally:
            conn.close()

        assert result[d_old["decision_id"]] == d_new["decision_id"]
        assert result[d_new["decision_id"]] is None

    def test_multiple_supersedes_returns_latest(self, topic_id):
        """複数 superseder があれば最新の 1 件 (created_at DESC の先頭) を返す"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_a = add_decision(decision="A", reason="r", topic_id=topic_id)
        d_b = add_decision(decision="B", reason="r", topic_id=topic_id)
        _link_supersede(d_a["decision_id"], d_old["decision_id"])
        _link_supersede(d_b["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            result = get_superseded_by_batch(conn, [d_old["decision_id"]])
        finally:
            conn.close()

        # d_b の supersede リレーションが後から張られたので、created_at DESC で d_b が最新
        assert result[d_old["decision_id"]] == d_b["decision_id"]
