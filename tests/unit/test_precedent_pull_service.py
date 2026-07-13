"""precedent_pull_service の統合テスト。

routing（topic_vec KNN・explicit指定・miss/unavailable）、browse保証（30件超の
全件列挙・retract除外・supersede保持・複数topic帰属の重複排除・material紐付け・
副作用なし）、予算縮退（全件index保証・full昇格の決定性）、レスポンス実サイズ
ゲート（一次予算内でも実サイズ超過時にfull→index降格・materialカタログ縮退）、
precedent_pure連携、flavor適用を検証する。
"""
import math
import sqlite3

import pytest
from sqlite_vec import serialize_float32

from src.config import PRECEDENT_RESPONSE_CHARS_MAX
from src.db import get_connection
from src.services import precedent_pull_service as pps
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision

DEFAULT_TAGS = ["domain:test"]
EMBEDDING_DIM = 384


@pytest.fixture(autouse=True)
def _clear_injected_tags():
    """temp_db フィクスチャは conftest.py 共通版を使う。ここでは各テスト前に
    tag_notes 注入済みマークだけをクリアする（conftest の temp_db は
    autouse ではないため、直接呼ばないテスト経路でも tags 状態が残らないようにする）。"""
    _injected_tags.clear()
    yield


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """add_topic経由の書込を成立させるための最小モック。

    書き込まれる値そのものはテストで使わない（routingの距離制御は
    precedent_pull_service.encode_query の直接monkeypatchで行うため）。
    """
    import src.services.embedding_service as emb

    def mock_encode_batch(texts, prefix):
        return [[0.001] * EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)


def _basis_vector(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _tilted_vector(index_a: int, index_b: int, cos_theta: float, dim: int = EMBEDDING_DIM) -> list[float]:
    """basis(index_a)からcos(theta)=cos_thetaだけ傾けた単位ベクトルを作る（cosine距離の制御用）。"""
    sin_theta = math.sqrt(max(0.0, 1.0 - cos_theta * cos_theta))
    v = [0.0] * dim
    v[index_a] = cos_theta
    v[index_b] = sin_theta
    return v


def _set_topic_vector(topic_id: int, vector: list[float]) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM topic_vec WHERE rowid = ?", (topic_id,))
        conn.execute(
            "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
            (topic_id, serialize_float32(vector)),
        )
        conn.commit()
    finally:
        conn.close()


def _make_topic(title: str, vector_index: int, description: str = "desc") -> int:
    topic_id = add_topic(title=title, description=description, tags=DEFAULT_TAGS)["topic_id"]
    _set_topic_vector(topic_id, _basis_vector(vector_index))
    return topic_id


def _decision(topic_id: int, decision: str = "d", reason: str = "r") -> int:
    result = add_decision(decision=decision, reason=reason, topic_id=topic_id, tags=DEFAULT_TAGS)
    assert "error" not in result, result
    return result["decision_id"]


def _link_supersede(newer_id: int, older_id: int) -> None:
    result = add_relation(
        "decision", newer_id, [{"type": "decision", "ids": [older_id]}], relation_type="supersedes"
    )
    assert "error" not in result, result


def _link_related(source_type: str, source_id: int, target_type: str, target_id: int) -> None:
    result = add_relation(source_type, source_id, [{"type": target_type, "ids": [target_id]}])
    assert "error" not in result, result


def _topic_by_id(result: dict, topic_id: int) -> dict:
    for t in result["topics"]:
        if t["topic_id_raw"] == topic_id:
            return t
    raise AssertionError(f"topic {topic_id} not found in result: {result}")


def _decision_by_id(topic_entry: dict, decision_id: int) -> dict:
    for d in topic_entry["decisions"]:
        if d["id_raw"] == decision_id:
            return d
    raise AssertionError(f"decision {decision_id} not found in topic: {topic_entry}")


# ========================================
# routing
# ========================================


class TestRouting:
    def test_topic_vec_isolated_from_other_entities(self, temp_db, mock_embedding_server, monkeypatch):
        """vec_indexに他entityのembeddingが大量にあっても、topic_vecのKNN母集団はtopicのみである"""
        topic_id = _make_topic("target-topic", 0)

        conn = get_connection()
        try:
            for i in range(50):
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                    (10_000 + i, serialize_float32(_basis_vector(0))),
                )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        conn = get_connection()
        try:
            routing = pps.route_topics("query", 3, conn)
        finally:
            conn.close()

        assert routing["mode"] == "vector"
        assert len(routing["candidates"]) == 1
        assert routing["candidates"][0]["topic_id"] == topic_id
        assert routing["candidates"][0]["distance"] == pytest.approx(0.0)

    def test_candidates_have_distance_and_top_k_selected(self, temp_db, mock_embedding_server, monkeypatch):
        """複数candidateが距離付きで返り、閾値内の上位k件だけがselectedになる"""
        # near: 距離0(閾値内)、mid: 閾値内(cos_theta=0.995→distance=0.005)、far: 直交(距離1.0、閾値超)
        near_topic = _make_topic("near", 0)
        mid_topic = _make_topic("mid", 5)
        far_topic = _make_topic("far", 2)
        _set_topic_vector(near_topic, _basis_vector(0))
        _set_topic_vector(mid_topic, _tilted_vector(0, 3, 0.995))
        _set_topic_vector(far_topic, _basis_vector(2))

        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        conn = get_connection()
        try:
            routing = pps.route_topics("query", 3, conn)
        finally:
            conn.close()

        by_id = {c["topic_id"]: c for c in routing["candidates"]}
        assert by_id[near_topic]["selected"] is True
        assert by_id[mid_topic]["selected"] is True
        assert by_id[far_topic]["selected"] is False
        assert by_id[far_topic]["distance"] > by_id[mid_topic]["distance"]

    def test_k_limits_selected_count(self, temp_db, mock_embedding_server, monkeypatch):
        """selectedはk件で打ち切られる（距離が近い順）"""
        topics = []
        for i in range(4):
            t = _make_topic(f"t{i}", i)
            _set_topic_vector(t, _basis_vector(0))  # 全て query と完全一致 (distance=0)
            topics.append(t)

        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        conn = get_connection()
        try:
            routing = pps.route_topics("query", 2, conn)
        finally:
            conn.close()

        selected = [c for c in routing["candidates"] if c["selected"]]
        assert len(selected) == 2

    def test_routing_miss_when_all_beyond_threshold(self, temp_db, mock_embedding_server, monkeypatch):
        """全candidateが閾値超のときguarantee=routing_missになり、例外にならない"""
        _make_topic("unrelated", 1)
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        result = pps.pull_precedents("これから決めたい論点についての文脈です")

        assert result["guarantee"] == "routing_miss"
        assert result["topics"] == []
        assert len(result["routing"]["candidates"]) >= 1

    def test_routing_unavailable_when_embedding_server_down_and_no_topic_ids(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """embeddingサーバー停止時（topic_ids未指定）はrouting_unavailableで即応答する"""
        monkeypatch.setattr(pps, "encode_query", lambda context: None)

        result = pps.pull_precedents("何らかの論点についての文脈")

        assert result["guarantee"] == "routing_unavailable"
        assert result["routing"]["mode"] == "unavailable"
        assert result["topics"] == []

    def test_knn_execute_failure_falls_back_to_unavailable(self, temp_db, mock_embedding_server, monkeypatch):
        """topic_vec の KNN クエリ自体が失敗（拡張未ロード・テーブル不整合等）しても
        例外にせず mode=unavailable に縮退する"""
        _make_topic("t", 0)
        monkeypatch.setattr(pps, "encode_query", lambda context: _basis_vector(0))

        class _RaisingConn:
            """topic_vec への MATCH クエリだけ失敗させ、他は実 conn に委譲するラッパー。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if "topic_vec" in sql:
                    raise sqlite3.OperationalError("no such module: vec0")
                return self._real.execute(sql, *args, **kwargs)

        conn = get_connection()
        try:
            routing = pps.route_topics("query", 3, _RaisingConn(conn))
        finally:
            conn.close()

        assert routing["mode"] == "unavailable"
        assert routing["candidates"] == []

    def test_topic_ids_explicit_works_even_when_embedding_server_down(
        self, temp_db, mock_embedding_server, monkeypatch
    ):
        """topic_ids明示指定時はembeddingサーバー停止でも通常動作する"""
        topic_id = _make_topic("anchored", 0)
        _decision(topic_id, "d1", "r1")
        monkeypatch.setattr(pps, "encode_query", lambda context: None)

        result = pps.pull_precedents("何らかの論点についての文脈", topic_ids=[topic_id])

        assert result["guarantee"] == "enumerated"
        assert result["routing"]["mode"] == "explicit"
        assert len(result["topics"]) == 1

    def test_explicit_topic_ids_skip_routing_and_mark_not_found(self, temp_db, mock_embedding_server):
        """topic_ids指定時はrouting(KNN)をスキップし、存在しないidはnot_foundで除外される"""
        topic_id = _make_topic("real-topic", 0)
        _decision(topic_id, "d1", "r1")
        missing_id = topic_id + 9999

        result = pps.pull_precedents("文脈テキスト", topic_ids=[topic_id, missing_id])

        assert result["routing"]["mode"] == "explicit"
        candidates = result["routing"]["candidates"]
        found = [c for c in candidates if c.get("topic_id_raw") == topic_id]
        not_found = [c for c in candidates if c.get("topic_id_raw") == missing_id]
        assert found[0]["selected"] is True
        assert not_found[0]["error"] == "not_found"
        assert len(result["topics"]) == 1

    def test_k_clamp_low_and_high(self, temp_db, mock_embedding_server):
        """kの0以下・6以上の入力はそれぞれ1・5にclampされる"""
        topic_ids = [_make_topic(f"topic{i}", i) for i in range(7)]
        for t in topic_ids:
            _decision(t, "d", "r")

        result_low = pps.pull_precedents("文脈", topic_ids=list(topic_ids), k=0)
        selected_low = [c for c in result_low["routing"]["candidates"] if c.get("selected")]
        assert len(selected_low) == 1

        result_high = pps.pull_precedents("文脈", topic_ids=list(topic_ids), k=100)
        selected_high = [c for c in result_high["routing"]["candidates"] if c.get("selected")]
        assert len(selected_high) == 5

    def test_context_validation_error(self, temp_db, mock_embedding_server):
        """contextが空/2文字未満はVALIDATION_ERRORになる"""
        result = pps.pull_precedents("")
        assert result["error"]["code"] == "VALIDATION_ERROR"

        result2 = pps.pull_precedents("a")
        assert result2["error"]["code"] == "VALIDATION_ERROR"


# ========================================
# browse保証
# ========================================


class TestBrowseGuarantee:
    def test_over_30_decisions_all_appear(self, temp_db, mock_embedding_server):
        """decision 30件超のtopicで、全非retract decisionがdecisions_total/出現件数として一致する"""
        topic_id = _make_topic("big-topic", 0)
        for i in range(40):
            _decision(topic_id, f"decision-{i}", f"reason-{i}")

        result = pps.pull_precedents("文脈", topic_ids=[topic_id])

        topic_entry = _topic_by_id(result, topic_id)
        assert topic_entry["decisions_total"] == 40
        assert len(topic_entry["decisions"]) == 40

    def test_retracted_decisions_excluded(self, temp_db, mock_embedding_server):
        """retract済みdecisionは列挙されない"""
        topic_id = _make_topic("t", 0)
        live_id = _decision(topic_id, "live", "r")
        dead_id = _decision(topic_id, "dead", "r")
        retract("decision", [dead_id])

        result = pps.pull_precedents("文脈", topic_ids=[topic_id])
        topic_entry = _topic_by_id(result, topic_id)
        ids = {d["id_raw"] for d in topic_entry["decisions"]}
        assert ids == {live_id}
        assert topic_entry["decisions_total"] == 1

    def test_superseded_decision_included_with_flags(self, temp_db, mock_embedding_server):
        """superseded decisionは除外されず、is_superseded/superseded_by/supersede_chainが付く"""
        topic_id = _make_topic("t", 0)
        old_id = _decision(topic_id, "old", "r")
        new_id = _decision(topic_id, "new", "r")
        _link_supersede(new_id, old_id)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id])
        topic_entry = _topic_by_id(result, topic_id)
        old_item = _decision_by_id(topic_entry, old_id)

        assert old_item["is_superseded"] is True
        assert old_item["superseded_by"] == new_id
        if old_item["detail"] == "full":
            assert set(old_item["supersede_chain"]) == {old_id, new_id}

    def test_multi_topic_decision_full_once_with_also_in(self, temp_db, mock_embedding_server):
        """複数topicにbelongs_toするdecisionは最初のtopicにのみ本文を置き、他方はindex+also_in"""
        topic_a = _make_topic("topic-a", 0)
        topic_b = _make_topic("topic-b", 1)
        decision_id = _decision(topic_a, "shared decision", "shared reason")
        _link_related("decision", decision_id, "topic", topic_b)

        result = pps.pull_precedents("文脈", topic_ids=[topic_a, topic_b])

        entry_a = _topic_by_id(result, topic_a)
        entry_b = _topic_by_id(result, topic_b)
        item_a = _decision_by_id(entry_a, decision_id)
        item_b = _decision_by_id(entry_b, decision_id)

        assert item_a["detail"] == "full"
        assert "also_in" not in item_a
        assert item_b["detail"] == "index"
        assert item_b["also_in"] == [topic_a]

    def test_material_linked_bidirectionally_and_retracted_excluded(self, temp_db, mock_embedding_server):
        """decision↔material の related エッジが material_ids / linked_decision_ids として双方向対応し、
        retract済みmaterialは除外される"""
        topic_id = _make_topic("t", 0)
        decision_id = _decision(topic_id, "d", "r")
        material_id = add_material(title="evidence", content="内容", tags=DEFAULT_TAGS, source="test")[
            "material_id"
        ]
        dead_material_id = add_material(
            title="dead-evidence", content="内容2", tags=DEFAULT_TAGS, source="test"
        )["material_id"]
        _link_related("decision", decision_id, "material", material_id)
        _link_related("decision", decision_id, "material", dead_material_id)
        retract("material", [dead_material_id])

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], include_materials=True)
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert item.get("material_ids") == [material_id]
        material_entries = {m["id_raw"]: m for m in topic_entry["materials"]}
        assert material_id in material_entries
        assert dead_material_id not in material_entries
        assert material_entries[material_id]["linked_decision_ids"] == [decision_id]

    def test_no_side_effects_on_other_tables(self, temp_db, mock_embedding_server):
        """呼び出し前後でactivities/decisions/materials/topics等のテーブルが不変（telemetryを除く）"""
        topic_id = _make_topic("t", 0)
        _decision(topic_id, "d", "r")

        conn = get_connection()
        try:
            before = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("decisions", "materials", "discussion_topics", "activities", "relations")
            }
        finally:
            conn.close()

        pps.pull_precedents("文脈", topic_ids=[topic_id])

        conn = get_connection()
        try:
            after = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("decisions", "materials", "discussion_topics", "activities", "relations")
            }
        finally:
            conn.close()

        assert before == after


# ========================================
# material 展開キャップ
# ========================================


class TestMaterialTruncation:
    def test_within_cap_not_truncated(self, temp_db, mock_embedding_server):
        """material が30件キャップ内なら materials_truncated=false で全件載る"""
        topic_id = _make_topic("t", 0)
        decision_id = _decision(topic_id, "d", "r")
        mids = []
        for i in range(3):
            mid = add_material(title=f"m{i}", content="c", tags=DEFAULT_TAGS, source="test")["material_id"]
            _link_related("decision", decision_id, "material", mid)
            mids.append(mid)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], include_materials=True)

        assert result["materials_truncated"] is False
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)
        assert set(item["material_ids"]) == set(mids)

    def test_over_cap_sets_materials_truncated_and_drops_excess(self, temp_db, mock_embedding_server):
        """related 経由の material が30件を超えると materials_truncated=true になり、
        載る material は30件以下に切り詰められる（黙って落とさず縮退を明示する）"""
        topic_id = _make_topic("t", 0)
        decision_id = _decision(topic_id, "d", "r")
        for i in range(40):
            mid = add_material(title=f"m{i}", content="c", tags=DEFAULT_TAGS, source="test")["material_id"]
            _link_related("decision", decision_id, "material", mid)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], include_materials=True)

        assert result["materials_truncated"] is True
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)
        assert len(item["material_ids"]) <= 30
        # decision 網羅保証（本文側）は material 縮退と独立
        assert topic_entry["decisions_total"] == 1
        assert result["truncated"] is False

    def test_materials_truncated_false_when_materials_disabled(self, temp_db, mock_embedding_server):
        """include_materials=false のときは materials_truncated は常に false"""
        topic_id = _make_topic("t", 0)
        decision_id = _decision(topic_id, "d", "r")
        for i in range(40):
            mid = add_material(title=f"m{i}", content="c", tags=DEFAULT_TAGS, source="test")["material_id"]
            _link_related("decision", decision_id, "material", mid)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], include_materials=False)

        assert result["materials_truncated"] is False


# ========================================
# 予算・縮退
# ========================================


class TestBudget:
    def test_sufficient_budget_all_full(self, temp_db, mock_embedding_server):
        """予算十分時は全件detail=full、truncated=false"""
        topic_id = _make_topic("t", 0)
        for i in range(5):
            _decision(topic_id, f"d{i}", "r" * 10)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=100_000)

        topic_entry = _topic_by_id(result, topic_id)
        assert all(d["detail"] == "full" for d in topic_entry["decisions"])
        assert result["truncated"] is False
        assert result["budget"]["index_only"] == 0

    def test_insufficient_budget_truncates_but_keeps_all_as_index(self, temp_db, mock_embedding_server):
        """予算不足時: full+indexの合計は常に全件、truncated=true、budget.used<=limit、full件数と一致"""
        topic_id = _make_topic("t", 0)
        ids = []
        for i in range(5):
            ids.append(_decision(topic_id, "d", "x" * 1000))

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=2500)

        topic_entry = _topic_by_id(result, topic_id)
        assert len(topic_entry["decisions"]) == 5
        assert topic_entry["decisions_total"] == 5
        full_count = sum(1 for d in topic_entry["decisions"] if d["detail"] == "full")
        index_count = sum(1 for d in topic_entry["decisions"] if d["detail"] == "index")
        assert full_count + index_count == 5
        assert result["truncated"] is True
        assert result["budget"]["used"] <= result["budget"]["limit"]
        assert result["budget"]["full"] == full_count

    def test_allocation_order_is_deterministic(self, temp_db, mock_embedding_server):
        """同一入力に対して同じfull/index割当が得られる（配分順の決定性）"""
        topic_id = _make_topic("t", 0)
        for i in range(6):
            _decision(topic_id, "d", "x" * 800)

        result1 = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=2500)
        result2 = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=2500)

        ids1 = sorted(d["id_raw"] for d in _topic_by_id(result1, topic_id)["decisions"] if d["detail"] == "full")
        ids2 = sorted(d["id_raw"] for d in _topic_by_id(result2, topic_id)["decisions"] if d["detail"] == "full")
        assert ids1 == ids2

    def test_non_superseded_promoted_before_superseded(self, temp_db, mock_embedding_server):
        """配分順: 非superseded(新しい順)を先に、supersededは後回しにする"""
        topic_id = _make_topic("t", 0)
        old_id = _decision(topic_id, "old", "x" * 500)
        new_id = _decision(topic_id, "new", "x" * 500)
        _link_supersede(new_id, old_id)
        # old_idはsuperseded、new_idは非superseded。予算は1件分ぎりぎりにする
        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=520)

        topic_entry = _topic_by_id(result, topic_id)
        new_item = _decision_by_id(topic_entry, new_id)
        old_item = _decision_by_id(topic_entry, old_id)
        assert new_item["detail"] == "full"
        assert old_item["detail"] == "index"

    def test_index_item_has_minimum_fields(self, temp_db, mock_embedding_server):
        """index行のみでもid/title/状態フラグ/created_atが読める"""
        topic_id = _make_topic("t", 0)
        _decision(topic_id, "d", "x" * 2000)
        _decision(topic_id, "d2", "x" * 2000)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=100)

        topic_entry = _topic_by_id(result, topic_id)
        index_items = [d for d in topic_entry["decisions"] if d["detail"] == "index"]
        assert index_items
        for item in index_items:
            assert "id_raw" in item
            assert "title" in item
            assert "created_at" in item
            assert "is_superseded" in item
            assert "superseded_by" in item


# ========================================
# レスポンス実サイズゲート
# ========================================


def _reason_with_sections(filler_len: int) -> str:
    """定型節を含み、指定文字数分のfillerでsections展開後のサイズを膨らませるreason。"""
    filler = "x" * filler_len
    return (
        f"却下案:\n- A案: {filler}\n"
        f"適用条件:\n- {filler}\n"
        f"適用外:\n- {filler}\n"
        f"検証:\n2026-01-01 実測 {filler}\n"
    )


class TestResponseSizeGate:
    def test_response_chars_present_and_zero_demoted_when_within_limit(
        self, temp_db, mock_embedding_server
    ):
        """通常時もbudget.response_chars（limit/measured/demoted）が常に付き、demoted=0"""
        topic_id = _make_topic("t", 0)
        _decision(topic_id, "d", "r")

        result = pps.pull_precedents("文脈", topic_ids=[topic_id])

        response_chars = result["budget"]["response_chars"]
        assert response_chars["limit"] == PRECEDENT_RESPONSE_CHARS_MAX
        assert response_chars["demoted"] == 0
        assert response_chars["measured"] > 0
        assert response_chars["measured"] <= PRECEDENT_RESPONSE_CHARS_MAX

    def test_sufficient_char_budget_but_oversized_response_demotes_full_items(
        self, temp_db, mock_embedding_server
    ):
        """decision+reasonの一次予算(budget_chars)には十分収まっても、tags/sections等の
        重複計上でレスポンス実サイズが上限を超える場合、full itemが配分逆順で
        indexへ降格され、実測サイズが上限以下に収まる"""
        topic_id = _make_topic("t", 0)
        for i in range(15):
            _decision(topic_id, f"decision-{i}", _reason_with_sections(300))

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=1_000_000)

        # 一次予算(budget_chars)は十分なので、char予算上は全件fullが配分されている
        assert result["budget"]["index_only"] == 0
        assert result["budget"]["full"] == 15

        response_chars = result["budget"]["response_chars"]
        assert response_chars["demoted"] > 0
        assert response_chars["measured"] <= PRECEDENT_RESPONSE_CHARS_MAX

        # だが実際のレスポンスでは降格分がindexになっている
        topic_entry = _topic_by_id(result, topic_id)
        actual_full_count = sum(1 for d in topic_entry["decisions"] if d["detail"] == "full")
        assert actual_full_count == 15 - response_chars["demoted"]
        assert result["truncated"] is True

    def test_all_decisions_remain_enumerated_after_demotion(self, temp_db, mock_embedding_server):
        """降格が発生しても全decisionはfull/indexいずれかとして応答に残る（網羅保証を壊さない）"""
        topic_id = _make_topic("t", 0)
        ids = [_decision(topic_id, f"decision-{i}", _reason_with_sections(300)) for i in range(15)]

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=1_000_000)

        topic_entry = _topic_by_id(result, topic_id)
        assert topic_entry["decisions_total"] == 15
        assert len(topic_entry["decisions"]) == 15
        assert {d["id_raw"] for d in topic_entry["decisions"]} == set(ids)

    def test_demotion_selection_is_deterministic(self, temp_db, mock_embedding_server):
        """同一入力に対して同じ降格結果(full/index割当)が得られる"""
        topic_id = _make_topic("t", 0)
        for i in range(15):
            _decision(topic_id, f"decision-{i}", _reason_with_sections(300))

        result1 = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=1_000_000)
        result2 = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=1_000_000)

        full_ids1 = sorted(
            d["id_raw"] for d in _topic_by_id(result1, topic_id)["decisions"] if d["detail"] == "full"
        )
        full_ids2 = sorted(
            d["id_raw"] for d in _topic_by_id(result2, topic_id)["decisions"] if d["detail"] == "full"
        )
        assert full_ids1 == full_ids2
        assert result1["budget"]["response_chars"]["demoted"] == result2["budget"]["response_chars"]["demoted"]

    def test_materials_catalog_degraded_when_demotion_alone_is_insufficient(
        self, temp_db, mock_embedding_server
    ):
        """decision本文がほぼ無くてもmaterialカタログ自体が巨大でレスポンスが超過する場合、
        materialsがcatalog_overflow相当(type/id_raw/titleのみ)へ縮退しmaterials_truncated=trueになる。
        decisionの網羅保証(decisions_total)はこの縮退と独立に維持される"""
        topic_id = _make_topic("t", 0)
        _decision(topic_id, "d", "r")
        for i in range(150):
            mid = add_material(
                title=f"material-{i}", content="c" * 200, tags=DEFAULT_TAGS, source="test"
            )["material_id"]
            _link_related("material", mid, "topic", topic_id)

        result = pps.pull_precedents(
            "文脈", topic_ids=[topic_id], budget_chars=100_000, include_materials=True
        )

        topic_entry = _topic_by_id(result, topic_id)
        assert len(topic_entry["materials"]) == 150
        for m in topic_entry["materials"]:
            assert set(m.keys()) == {"type", "id_raw", "title"}
            assert m["type"] == "material"
        assert result["materials_truncated"] is True
        assert result["budget"]["response_chars"]["measured"] <= PRECEDENT_RESPONSE_CHARS_MAX
        assert topic_entry["decisions_total"] == 1


# ========================================
# precedent_pure連携
# ========================================


class TestPrecedentSections:
    def test_sections_attached_when_present(self, temp_db, mock_embedding_server):
        """定型節ありdecisionにsectionsが付く"""
        topic_id = _make_topic("t", 0)
        reason = "却下案:\n- A案: コストが高い\n適用条件:\n- 小規模プロジェクト\n検証:\n2026-01-01 実測\n"
        decision_id = _decision(topic_id, "採用: B案", reason)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=100_000)
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert item["detail"] == "full"
        assert "sections" in item
        assert item["sections"]["rejected_alternatives"] == [{"alternative": "A案", "reason": "コストが高い"}]
        assert item["sections"]["scope_in"] == ["小規模プロジェクト"]

    def test_sections_omitted_when_absent(self, temp_db, mock_embedding_server):
        """定型節なしdecisionにはsectionsキー自体が無い"""
        topic_id = _make_topic("t", 0)
        decision_id = _decision(topic_id, "普通の決定", "普通の理由")

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=100_000)
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert "sections" not in item

    def test_warnings_pass_through_without_breaking_enumeration(self, temp_db, mock_embedding_server):
        """パーサwarningsがsections.warningsとして素通しされ、列挙・予算配分は正常に完了する"""
        topic_id = _make_topic("t", 0)
        reason = "却下案:\n\n検証: コミットのみで日付なし\n"
        decision_id = _decision(topic_id, "d", reason)

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=100_000)
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert item["detail"] == "full"
        assert "sections" in item
        assert len(item["sections"]["warnings"]) >= 1


# ========================================
# flavor
# ========================================


class TestFlavorInService:
    """precedent_pull_service自体はflavorを適用しない（main.py側の責務）ことを確認する。"""

    def test_service_layer_returns_raw_citation_templates(self, temp_db, mock_embedding_server):
        topic_id = _make_topic("t", 0)
        material_id = add_material(title="根拠資料", content="内容", tags=DEFAULT_TAGS, source="test")[
            "material_id"
        ]
        decision_id = _decision(topic_id, "d", f"理由 {{{{cite:M#{material_id}}}}}")

        result = pps.pull_precedents("文脈", topic_ids=[topic_id], budget_chars=100_000)
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert "{{cite:M#" in item["reason"]


class TestFlavorAtMcpToolLayer:
    """main.py の pull_precedents ツール本体（flavor適用の実責務）を検証する。"""

    def test_readable_flavor_expands_citation_without_id(self, temp_db, mock_embedding_server):
        from src.main import pull_precedents as mcp_pull_precedents

        topic_id = _make_topic("t", 0)
        material_id = add_material(title="根拠資料", content="内容", tags=DEFAULT_TAGS, source="test")[
            "material_id"
        ]
        decision_id = _decision(topic_id, "d", f"理由 {{{{cite:M#{material_id}}}}}")

        result = mcp_pull_precedents(
            "文脈", topic_ids=[topic_id], budget_chars=100_000, flavor="readable"
        )
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert "根拠資料" in item["reason"]
        assert "M#" not in item["reason"]
        assert "citations_out" in item

    def test_internal_flavor_keeps_id_in_citation_expansion(self, temp_db, mock_embedding_server):
        from src.main import pull_precedents as mcp_pull_precedents

        topic_id = _make_topic("t", 0)
        material_id = add_material(title="根拠資料", content="内容", tags=DEFAULT_TAGS, source="test")[
            "material_id"
        ]
        decision_id = _decision(topic_id, "d", f"理由 {{{{cite:M#{material_id}}}}}")

        result = mcp_pull_precedents(
            "文脈", topic_ids=[topic_id], budget_chars=100_000, flavor="internal"
        )
        topic_entry = _topic_by_id(result, topic_id)
        item = _decision_by_id(topic_entry, decision_id)

        assert "根拠資料" in item["reason"]
        assert "M#" in item["reason"]

    def test_index_item_title_gets_flavor_expanded(self, temp_db, mock_embedding_server):
        """detail=index の decision も title の citation テンプレは展開される（citations付与はしない）"""
        from src.main import pull_precedents as mcp_pull_precedents

        topic_id = _make_topic("t", 0)
        material_id = add_material(title="根拠資料", content="内容", tags=DEFAULT_TAGS, source="test")[
            "material_id"
        ]
        _decision(topic_id, f"タイトル {{{{cite:M#{material_id}}}}}", "x" * 2000)
        _decision(topic_id, "d2", "x" * 2000)

        result = mcp_pull_precedents(
            "文脈", topic_ids=[topic_id], budget_chars=100, flavor="readable"
        )
        topic_entry = _topic_by_id(result, topic_id)
        index_items = [d for d in topic_entry["decisions"] if d["detail"] == "index"]
        assert any("根拠資料" in d["title"] for d in index_items)
        assert "citations_out" not in index_items[0]
