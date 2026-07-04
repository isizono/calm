"""staleness_service（アンカー抽出・chain head 算出・staleness 付与）の単体テスト"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.db import get_connection, init_database
from src.services.relation_service import add_relation
from src.services.staleness_service import (
    _extract_anchors,
    annotate_staleness,
    get_chain_heads_batch,
)
from src.services.supersede_service import get_superseded_by_batch
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
    t = add_topic(title="staleness テスト", description="Desc", tags=DEFAULT_TAGS)
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


class TestExtractAnchors:
    """_extract_anchors: precedent_pure のパース結果からアンカーのみ取り出す薄いヘルパー"""

    def test_no_precedent_markers_returns_empty(self):
        assert _extract_anchors("自由記述のみの理由本文") == []

    def test_empty_text_returns_empty(self):
        assert _extract_anchors("") == []
        assert _extract_anchors(None) == []

    def test_markers_without_verify_section_returns_empty(self):
        """却下案:等はあるが検証:が無ければ空リスト"""
        reason = "却下案:\n- 案A: 理由A\n"
        assert _extract_anchors(reason) == []

    def test_commit_only(self):
        reason = "検証: commit 0123456789abcdef で確認\n"
        anchors = _extract_anchors(reason)
        assert len(anchors) == 1
        assert anchors[0]["commit"] == "0123456789abcdef"
        assert anchors[0]["date"] is None

    def test_date_only(self):
        reason = "検証: 実機確認のみ、日付は2026-07-04\n"
        anchors = _extract_anchors(reason)
        assert len(anchors) == 1
        assert anchors[0]["date"] == "2026-07-04"
        assert anchors[0]["commit"] is None

    def test_all_keys_present(self):
        reason = "検証: 実機確認 / abcdef1 / 2026-07-04\n"
        anchors = _extract_anchors(reason)
        assert len(anchors) == 1
        anchor = anchors[0]
        assert anchor["raw"] == "実機確認 / abcdef1 / 2026-07-04"
        assert anchor["date"] == "2026-07-04"
        assert anchor["commit"] == "abcdef1"

    def test_multiple_verify_lines(self):
        reason = "検証: 初回確認 / 2026-07-01\n検証: 再検証 / 2026-07-04\n"
        anchors = _extract_anchors(reason)
        assert len(anchors) == 2
        assert anchors[0]["date"] == "2026-07-01"
        assert anchors[1]["date"] == "2026-07-04"

    def test_missing_commit_and_date_does_not_raise(self):
        """hex にもならず日付形式にも合致しない検証行は、例外を出さず date/commit=None のまま残す"""
        reason = "検証: 目視での確認のみ\n"
        anchors = _extract_anchors(reason)
        assert len(anchors) == 1
        assert anchors[0]["date"] is None
        assert anchors[0]["commit"] is None

    def test_invalid_hex_too_short_treated_as_no_commit(self):
        """7桁未満のhex風文字列はcommitとして採用されない"""
        reason = "検証: bugfix ab12cd で確認\n"
        anchors = _extract_anchors(reason)
        assert anchors[0]["commit"] is None

    def test_invalid_date_format_treated_as_no_date(self):
        """YYYY-MM-DD以外の日付表記は日付として採用されない"""
        reason = "検証: 2026/07/04 に確認\n"
        anchors = _extract_anchors(reason)
        assert anchors[0]["date"] is None


class TestGetChainHeadsBatch:
    """get_chain_heads_batch: supersede chain の推移的な最新端"""

    def test_empty_input_returns_empty_map(self, temp_db):
        conn = get_connection()
        try:
            assert get_chain_heads_batch(conn, []) == {}
        finally:
            conn.close()

    def test_no_supersede_returns_self(self, topic_id):
        """supersede関係が無いdecisionは自身のみがhead"""
        d = add_decision(decision="独立", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            result = get_chain_heads_batch(conn, [d["decision_id"]])
        finally:
            conn.close()
        assert result[d["decision_id"]] == [d["decision_id"]]

    def test_linear_chain_has_unique_head(self, topic_id):
        """直線chain (old -> mid -> new) はどのノードから見てもheadはnew唯一"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_mid = add_decision(decision="中", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_mid["decision_id"], d_old["decision_id"])
        _link_supersede(d_new["decision_id"], d_mid["decision_id"])

        conn = get_connection()
        try:
            result = get_chain_heads_batch(
                conn, [d_old["decision_id"], d_mid["decision_id"], d_new["decision_id"]]
            )
        finally:
            conn.close()

        assert result[d_old["decision_id"]] == [d_new["decision_id"]]
        assert result[d_mid["decision_id"]] == [d_new["decision_id"]]
        assert result[d_new["decision_id"]] == [d_new["decision_id"]]

    def test_diamond_dag_has_unique_head(self, topic_id):
        """菱形DAG (old -> a, old -> b、a -> merged, b -> merged) はheadがmerged唯一"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_a = add_decision(decision="A", reason="r", topic_id=topic_id)
        d_b = add_decision(decision="B", reason="r", topic_id=topic_id)
        d_merged = add_decision(decision="統合", reason="r", topic_id=topic_id)
        _link_supersede(d_a["decision_id"], d_old["decision_id"])
        _link_supersede(d_b["decision_id"], d_old["decision_id"])
        _link_supersede(d_merged["decision_id"], d_a["decision_id"])
        _link_supersede(d_merged["decision_id"], d_b["decision_id"])

        conn = get_connection()
        try:
            result = get_chain_heads_batch(conn, [d_old["decision_id"]])
        finally:
            conn.close()

        assert result[d_old["decision_id"]] == [d_merged["decision_id"]]

    def test_multi_head_dag_returns_all_heads(self, topic_id):
        """oldがaとbの両方に独立にsupersedeされ、a/bとも以降supersedeされなければheadは両方"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_a = add_decision(decision="A", reason="r", topic_id=topic_id)
        d_b = add_decision(decision="B", reason="r", topic_id=topic_id)
        _link_supersede(d_a["decision_id"], d_old["decision_id"])
        _link_supersede(d_b["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            result = get_chain_heads_batch(conn, [d_old["decision_id"]])
        finally:
            conn.close()

        assert set(result[d_old["decision_id"]]) == {
            d_a["decision_id"],
            d_b["decision_id"],
        }

    def test_retracted_head_still_returned(self, topic_id):
        """retract済みでもchain head候補からは除外しない（意味判定はしない）"""
        from src.services.retract_service import retract

        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])
        retract("decision", [d_new["decision_id"]])

        conn = get_connection()
        try:
            result = get_chain_heads_batch(conn, [d_old["decision_id"]])
        finally:
            conn.close()

        assert result[d_old["decision_id"]] == [d_new["decision_id"]]


class TestAnnotateStaleness:
    """annotate_staleness: decision item群へのstalenessブロックin-place付与"""

    def test_empty_items_does_not_raise(self, temp_db):
        conn = get_connection()
        try:
            annotate_staleness(conn, [])
        finally:
            conn.close()

    def test_not_superseded_item(self, topic_id):
        d = add_decision(decision="独立", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            items = [{"id": d["decision_id"], "created_at": _now_str()}]
            annotate_staleness(conn, items)
        finally:
            conn.close()

        staleness = items[0]["staleness"]
        assert staleness["is_superseded"] is False
        assert staleness["superseded_by"] is None
        assert staleness["chain_heads"] == [d["decision_id"]]
        assert staleness["age_days"] == 0

    def test_superseded_by_matches_get_superseded_by_batch(self, topic_id):
        """is_superseded / superseded_by は get_superseded_by_batch の1hop規則と一致する"""
        d_old = add_decision(decision="古", reason="r", topic_id=topic_id)
        d_new = add_decision(decision="新", reason="r", topic_id=topic_id)
        _link_supersede(d_new["decision_id"], d_old["decision_id"])

        conn = get_connection()
        try:
            items = [{"id": d_old["decision_id"], "created_at": _now_str()}]
            annotate_staleness(conn, items)
            expected = get_superseded_by_batch(conn, [d_old["decision_id"]])
        finally:
            conn.close()

        staleness = items[0]["staleness"]
        assert staleness["superseded_by"] == expected[d_old["decision_id"]]
        assert staleness["is_superseded"] is True
        assert staleness["chain_heads"] == [d_new["decision_id"]]

    def test_age_days_computed_from_created_at(self, topic_id):
        d = add_decision(decision="古い決定", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            created_at = "2026-06-01 00:00:00"
            conn.execute(
                "UPDATE decisions SET created_at = ? WHERE id = ?",
                (created_at, d["decision_id"]),
            )
            conn.commit()
            items = [{"id": d["decision_id"], "created_at": created_at}]
            now = datetime(2026, 6, 15, tzinfo=timezone.utc)
            annotate_staleness(conn, items, now=now)
        finally:
            conn.close()

        assert items[0]["staleness"]["age_days"] == 14

    def test_item_without_reason_omits_anchors_key(self, topic_id):
        """reasonキーが無いitemはanchorsキーを省略し、例外も出さない"""
        d = add_decision(decision="独立", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            items = [{"id": d["decision_id"], "created_at": _now_str()}]
            annotate_staleness(conn, items)
        finally:
            conn.close()

        assert "anchors" not in items[0]["staleness"]

    def test_item_with_reason_includes_anchors(self, topic_id):
        d = add_decision(decision="独立", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            items = [
                {
                    "id": d["decision_id"],
                    "created_at": _now_str(),
                    "reason": "検証: 実機確認 / abcdef1 / 2026-07-04\n",
                }
            ]
            annotate_staleness(conn, items)
        finally:
            conn.close()

        anchors = items[0]["staleness"]["anchors"]
        assert len(anchors) == 1
        assert anchors[0]["commit"] == "abcdef1"

    def test_item_with_reason_but_no_anchor_gives_empty_anchors(self, topic_id):
        d = add_decision(decision="独立", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            items = [
                {
                    "id": d["decision_id"],
                    "created_at": _now_str(),
                    "reason": "定型節が無い自由記述の理由",
                }
            ]
            annotate_staleness(conn, items)
        finally:
            conn.close()

        assert items[0]["staleness"]["anchors"] == []

    def test_multiple_items_batched(self, topic_id):
        """複数itemを一括処理してもそれぞれ独立したstalenessが付く"""
        d1 = add_decision(decision="決定1", reason="r", topic_id=topic_id)
        d2 = add_decision(decision="決定2", reason="r", topic_id=topic_id)
        conn = get_connection()
        try:
            items = [
                {"id": d1["decision_id"], "created_at": _now_str()},
                {"id": d2["decision_id"], "created_at": _now_str()},
            ]
            annotate_staleness(conn, items)
        finally:
            conn.close()

        assert items[0]["staleness"]["chain_heads"] == [d1["decision_id"]]
        assert items[1]["staleness"]["chain_heads"] == [d2["decision_id"]]


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
