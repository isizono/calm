"""記録=クエリ添付（search_service.find_related_records / build_related_records_manifest）のテスト

検証項目:
1. find_related_records: entity_typesでの絞り込み、topic_idでのdecisionスコープ、
   excludeでの除外、distance昇順での返却
2. build_related_records_manifest: 類似度閾値未満の候補は含まれない
3. 呼び出し全体（複数created_items）で上位3件に絞られ、同じ既存記録が複数の
   created_itemsにヒットしたら類似度最大の1行にまとまる
4. セッション内で既に提示済みの記録は再提示されない（caller_session_id=Noneのときは
   重複排除が働かない）
5. 同一呼び出し（バッチ）で作った記録は候補から除外される
6. title/snippetがそれぞれの上限文字数で切り詰められる
7. injection_telemetryのpresent行が実際に採用した分だけ、rank/similarity/source_idが
   正しく書かれる
"""
import math

import numpy as np
import pytest

from src import config
from src.db import get_connection
from src.services import embedding_service as emb
from src.services import search_service
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.material_service import add_material
from src.services.topic_service import add_topic

EMBEDDING_DIM = 384
DEFAULT_TAGS = ["domain:test"]


def _unit_vector(index: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[index] = 1.0
    return v


def _mix(similarity: float, primary: int = 0, secondary: int = 1) -> list[float]:
    """primary軸との cosine類似度が厳密に `similarity` になる単位ベクトルを作る。"""
    v = [0.0] * EMBEDDING_DIM
    v[primary] = similarity
    v[secondary] = math.sqrt(max(0.0, 1.0 - similarity ** 2))
    return v


ANCHOR_VEC = _unit_vector(0)


@pytest.fixture
def controlled_embeddings(monkeypatch):
    """本文に含まれるマーカー文字列に応じて厳密なcosine類似度を持つベクトルを割り当てる
    モック。登録の無いテキストはハッシュシードの決定的ランダムベクトルにフォールバックする。
    """
    registry: dict[str, list[float]] = {}

    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            hit = None
            for marker, vec in registry.items():
                if marker in text:
                    hit = vec
                    break
            if hit is not None:
                embeddings.append(hit)
            else:
                np.random.seed(hash(prefix + text) % (2**32))
                embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)
    return registry


@pytest.fixture(autouse=True)
def reset_presented_records():
    """各テスト前後でセッション内既出集合をリセットする（テスト間の汚染防止）"""
    search_service._presented_records.clear()
    yield
    search_service._presented_records.clear()


@pytest.fixture(autouse=True)
def _no_implicit_embedding_backfill(monkeypatch):
    """embeddingサーバー接続成立時に自動起動するバックフィルスレッドを本ファイルでは
    起動させない。このスレッドはプロセスで1回だけ起動されてjoinされず、DBパスを
    接続のたびに環境変数から解決するため、起動したテストの終了後もtemp_dbが切り替えた
    次のテストのDBへ書き込みうる（database is locked / disk I/O error の原因）。
    本ファイルは1テストあたり複数回のadd_decisions/add_logs呼出でembedding生成を
    多用するため、この既知の競合を明示的に断つ。
    """
    monkeypatch.setattr(emb, "_backfill_done", True)


@pytest.fixture
def topic(temp_db):
    return add_topic(title="関連記録テスト", description="d", tags=DEFAULT_TAGS)


@pytest.fixture(autouse=True)
def capture_injection_telemetry_threads(monkeypatch):
    """_record_injection_telemetry_async が起動したthread群を捕捉してjoin()できるように
    する。build_related_records_manifestのpresent書込は非同期のため、書込を検証する
    テストはこのfixtureが返すリストで明示的にthreadの完了を待ってからDBを読まないと
    レース条件になる。autouseかつテスト終了時にも待ち合わせるのは、待ち合わせ忘れの
    threadがtemp_db teardown（一時ディレクトリ削除）と競合してdisk I/O error /
    ディレクトリ削除失敗を起こすのを防ぐため（本ファイルは1テストあたり複数回の
    add_decisions/add_logs呼出でこの経路を多用する）。"""
    threads = []
    original = search_service._record_injection_telemetry_async

    def wrapped(*args, **kwargs):
        started = original(*args, **kwargs)
        threads.extend(started)
        return started

    monkeypatch.setattr(search_service, "_record_injection_telemetry_async", wrapped)
    yield threads
    _wait_for_telemetry(threads)


def _wait_for_telemetry(threads, timeout=5.0):
    for t in threads:
        if t is not None:
            t.join(timeout=timeout)


def _injection_telemetry_rows() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT trigger_tool, source_type, source_id, attached_type, attached_id, "
            "rank, similarity, caller_session_id FROM injection_telemetry ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class TestFindRelatedRecordsKnn:
    """find_related_records単体の挙動"""

    def test_entity_types_filters_candidates(self, topic, controlled_embeddings):
        """entity_typesに含まれないtypeは候補に現れない"""
        controlled_embeddings["MARKER_MAT"] = _mix(0.9)
        mat = add_material(
            title="関連資材", content="MARKER_MAT 本文", tags=DEFAULT_TAGS, source="test",
        )

        results = search_service.find_related_records(
            embedding=_mix(0.9), entity_types=["decision"], exclude=set(),
        )
        result_types = {r["type"] for r in results}
        assert "material" not in result_types

        results_material = search_service.find_related_records(
            embedding=_mix(0.9), entity_types=["material"], exclude=set(),
        )
        result_ids = [r["id"] for r in results_material]
        assert mat["material_id"] in result_ids

    def test_exclude_removes_candidate(self, topic, controlled_embeddings):
        """excludeで指定した(type, id)は候補から除外される"""
        controlled_embeddings["MARKER_EXC"] = _mix(0.9)
        created = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_EXC", "reason": "r"},
        ])
        did = created["created"][0]["decision_id"]

        without_exclude = search_service.find_related_records(
            embedding=ANCHOR_VEC, entity_types=["decision"], exclude=set(),
        )
        assert did in [r["id"] for r in without_exclude]

        with_exclude = search_service.find_related_records(
            embedding=ANCHOR_VEC, entity_types=["decision"], exclude={("decision", did)},
        )
        assert did not in [r["id"] for r in with_exclude]

    def test_topic_id_scopes_decision_candidates(self, topic, controlled_embeddings):
        """topic_id指定時は別topicのdecisionが候補に出ない"""
        other_topic = add_topic(title="別トピック", description="d", tags=DEFAULT_TAGS)
        controlled_embeddings["MARKER_OTHER_TOPIC"] = _mix(0.9)
        other = add_decisions([
            {"topic_id": other_topic["topic_id"], "decision": "MARKER_OTHER_TOPIC", "reason": "r"},
        ])
        other_id = other["created"][0]["decision_id"]

        results = search_service.find_related_records(
            embedding=ANCHOR_VEC, entity_types=["decision"], exclude=set(),
            topic_id=topic["topic_id"],
        )
        assert other_id not in [r["id"] for r in results]

    def test_empty_entity_types_returns_empty(self, topic):
        """entity_types=[]は空リストを返す（embeddingを使わず早期return）"""
        assert search_service.find_related_records(
            embedding=ANCHOR_VEC, entity_types=[], exclude=set(),
        ) == []


class TestBuildRelatedRecordsManifestThreshold:
    def test_below_threshold_excluded_above_threshold_included(self, topic, controlled_embeddings):
        """類似度がRELATED_RECORDS_SIMILARITY_THRESHOLD未満の候補はmanifestに含まれない"""
        controlled_embeddings["MARKER_HIGH"] = _mix(0.9)
        controlled_embeddings["MARKER_LOW"] = _mix(0.3)
        high = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_HIGH", "reason": "r"},
        ])
        low = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_LOW", "reason": "r"},
        ])
        high_id = high["created"][0]["decision_id"]
        low_id = low["created"][0]["decision_id"]

        controlled_embeddings["MARKER_ANCHOR"] = ANCHOR_VEC
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_ANCHOR", "reason": "r"},
        ])

        related_ids = [r["id"] for r in result["related_decisions"]]
        assert high_id in related_ids, "閾値以上の候補がmanifestから漏れている"
        assert low_id not in related_ids, "閾値未満の候補がmanifestに混入している"


class TestBuildRelatedRecordsManifestBudget:
    def test_multiple_created_items_share_top3_and_dedup_by_max_similarity(
        self, topic, controlled_embeddings, capture_injection_telemetry_threads
    ):
        """複数created_itemsをまたいで呼び出し全体で上位3件に絞られ、同じ既存記録が
        複数created_itemsにヒットしたら1行にまとまる（類似度最大値を採用）"""
        controlled_embeddings["MARKER_EXISTING"] = _mix(0.9)
        existing = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_EXISTING", "reason": "r"},
        ])
        existing_id = existing["created"][0]["decision_id"]

        # 2件のdecisionをバッチ作成。両方ともMARKER_EXISTINGに近いベクトルを持つが
        # 2件目のほうが類似度が高い設定にする。
        controlled_embeddings["MARKER_NEW_A"] = _mix(0.8)
        controlled_embeddings["MARKER_NEW_B"] = _mix(0.85)
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_NEW_A", "reason": "r"},
            {"topic_id": topic["topic_id"], "decision": "MARKER_NEW_B", "reason": "r"},
        ])

        related = result["related_decisions"]
        matches = [r for r in related if r["id"] == existing_id]
        assert len(matches) == 1, "同じ既存記録が複数created_itemsにヒットしても1行にまとまるはず"

        _wait_for_telemetry(capture_injection_telemetry_threads)
        rows = _injection_telemetry_rows()
        present_rows_for_existing = [r for r in rows if r["attached_id"] == existing_id]
        assert len(present_rows_for_existing) == 1, (
            "present行も、採用された類似度最大の1件のみ書かれるはず"
        )
        assert present_rows_for_existing[0]["source_id"] == result["created"][1]["decision_id"], (
            "類似度が高い方（2件目=MARKER_NEW_B）がsource_idとして採用されるはず"
        )

    def test_related_records_never_exceed_top_n(self, topic, controlled_embeddings):
        """候補が4件以上あってもmanifestはRELATED_RECORDS_TOP_N件に絞られる"""
        for i in range(5):
            marker = f"MARKER_PRE_{i}"
            controlled_embeddings[marker] = _mix(0.9 - i * 0.01)
            add_decisions([{"topic_id": topic["topic_id"], "decision": marker, "reason": "r"}])

        controlled_embeddings["MARKER_TRIGGER"] = ANCHOR_VEC
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_TRIGGER", "reason": "r"},
        ])
        assert len(result["related_decisions"]) == config.RELATED_RECORDS_TOP_N


class TestBuildRelatedRecordsManifestSessionDedup:
    def test_same_session_does_not_repeat_presented_record(self, topic, controlled_embeddings):
        """同一caller_session_idでは、一度提示した記録を2回目の呼び出しで再提示しない"""
        controlled_embeddings["MARKER_TARGET"] = _mix(0.9)
        target = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_TARGET", "reason": "r"},
        ])
        target_id = target["created"][0]["decision_id"]

        controlled_embeddings["MARKER_Q1"] = ANCHOR_VEC
        controlled_embeddings["MARKER_Q2"] = ANCHOR_VEC

        first = add_decisions(
            [{"topic_id": topic["topic_id"], "decision": "MARKER_Q1", "reason": "r"}],
            caller_session_id="sess-dedup-1",
        )
        assert target_id in [r["id"] for r in first["related_decisions"]]

        second = add_decisions(
            [{"topic_id": topic["topic_id"], "decision": "MARKER_Q2", "reason": "r"}],
            caller_session_id="sess-dedup-1",
        )
        assert target_id not in [r["id"] for r in second["related_decisions"]], (
            "同一セッションで一度提示した記録が再提示されている"
        )

    def test_none_session_id_does_not_dedup(self, topic, controlled_embeddings):
        """caller_session_id=Noneのときは重複排除が働かず、毎回同じ記録が提示されうる"""
        controlled_embeddings["MARKER_TARGET2"] = _mix(0.9)
        target = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_TARGET2", "reason": "r"},
        ])
        target_id = target["created"][0]["decision_id"]

        controlled_embeddings["MARKER_Q3"] = ANCHOR_VEC
        controlled_embeddings["MARKER_Q4"] = ANCHOR_VEC

        first = add_decisions(
            [{"topic_id": topic["topic_id"], "decision": "MARKER_Q3", "reason": "r"}],
        )
        second = add_decisions(
            [{"topic_id": topic["topic_id"], "decision": "MARKER_Q4", "reason": "r"}],
        )
        assert target_id in [r["id"] for r in first["related_decisions"]]
        assert target_id in [r["id"] for r in second["related_decisions"]], (
            "caller_session_id未指定なのに2回目で除外されている（重複排除が誤って働いている）"
        )


class TestBuildRelatedRecordsManifestBatchExclusion:
    def test_same_batch_cross_type_records_excluded(self, topic, controlled_embeddings):
        """add_logsの同一呼び出し内で作った複数logは互いの候補から除外される"""
        controlled_embeddings["MARKER_LOG_A"] = _mix(0.95)
        controlled_embeddings["MARKER_LOG_B"] = _mix(0.95)
        result = add_logs([
            {"topic_id": topic["topic_id"], "content": "MARKER_LOG_A", "title": "A"},
            {"topic_id": topic["topic_id"], "content": "MARKER_LOG_B", "title": "B"},
        ])
        first_id = result["created"][0]["log_id"]
        second_id = result["created"][1]["log_id"]

        related_ids = [r["id"] for r in result["related_records"]]
        assert first_id not in related_ids
        assert second_id not in related_ids


class TestBuildRelatedRecordsManifestTruncation:
    def test_title_and_snippet_are_truncated(self, topic, controlled_embeddings, monkeypatch):
        """titleとsnippetがそれぞれの上限文字数で切り詰められる"""
        monkeypatch.setattr(config, "RELATED_RECORDS_TITLE_MAX_LEN", 5)
        monkeypatch.setattr(config, "RELATED_RECORDS_SNIPPET_MAX_LEN", 8)

        long_title = "長いタイトルです" * 3
        long_decision = "MARKER_LONG " + ("本文が長い記録です" * 5)
        controlled_embeddings["MARKER_LONG"] = _mix(0.9)
        add_decisions([
            {"topic_id": topic["topic_id"], "decision": long_decision, "reason": "r", "title": long_title},
        ])

        controlled_embeddings["MARKER_TRUNC_ANCHOR"] = ANCHOR_VEC
        result = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_TRUNC_ANCHOR", "reason": "r"},
        ])

        assert len(result["related_decisions"]) == 1
        entry = result["related_decisions"][0]
        assert entry["title"] == long_title[:5]
        assert len(entry["snippet"]) <= 8


class TestInjectionTelemetryPresentRows:
    def test_present_rows_match_manifest(
        self, topic, controlled_embeddings, capture_injection_telemetry_threads
    ):
        """injection_telemetryのpresent行が、実際にmanifestへ採用された分だけ
        rank/similarity/source_id/attached_id/trigger_toolを正しく持つ"""
        controlled_embeddings["MARKER_PRESENT_TARGET"] = _mix(0.9)
        target = add_decisions([
            {"topic_id": topic["topic_id"], "decision": "MARKER_PRESENT_TARGET", "reason": "r"},
        ])
        target_id = target["created"][0]["decision_id"]

        controlled_embeddings["MARKER_PRESENT_TRIGGER"] = ANCHOR_VEC
        result = add_decisions(
            [{"topic_id": topic["topic_id"], "decision": "MARKER_PRESENT_TRIGGER", "reason": "r"}],
            caller_session_id="sess-present-1",
        )
        trigger_id = result["created"][0]["decision_id"]

        _wait_for_telemetry(capture_injection_telemetry_threads)
        rows = _injection_telemetry_rows()
        matching = [r for r in rows if r["attached_id"] == target_id]
        assert len(matching) == 1
        row = matching[0]
        assert row["trigger_tool"] == "add_decisions"
        assert row["source_type"] == "decision"
        assert row["source_id"] == trigger_id
        assert row["attached_type"] == "decision"
        assert row["rank"] == 1
        assert row["similarity"] == pytest.approx(0.9, abs=1e-3)
        assert row["caller_session_id"] == "sess-present-1"
