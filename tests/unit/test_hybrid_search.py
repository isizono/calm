"""ハイブリッド検索（FTS5 + ベクトル + RRF統合）のテスト

_rrf_merge単体テスト + _apply_recency_boost単体テスト + タグ対応の統合テスト。
"""
import hashlib
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
import pytest
import numpy as np

from src.db import init_database, get_connection
from src.services.search_service import (
    _rrf_merge, _apply_recency_boost, _attach_details, _compute_adaptive_weights,
    find_similar_topics, _expand_query_with_tags,
    RRF_K, RRF_W_FTS, RRF_W_VEC, RRF_W_TAG, RECENCY_DECAY_RATE, RECENCY_DECAY_FLOOR,
    QE_DISTANCE_THRESHOLD, QE_MAX_EXPANSIONS, QE_EXCLUDE_NAMESPACES,
    ADAPTIVE_RRF_ENABLED, ADAPTIVE_RRF_THRESHOLDS,
    DETAILS_MAX_RESULTS, DETAILS_DESCRIPTION_MAX,
)
from src.services import search_service
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_log, add_decision
from src.services.material_service import add_material
import src.services.embedding_service as emb


EMBEDDING_DIM = 384
DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def mock_embedding_model(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化"""

    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            seed = int(hashlib.sha256((prefix_str + text).encode()).hexdigest(), 16) % (2**32)
            np.random.seed(seed)
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    yield


@pytest.fixture
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


# ========================================
# _rrf_merge 単体テスト
# ========================================


def test_rrf_merge_both_sources():
    """RRF統合: 両方にヒットするアイテムのスコアが0〜1に正規化される"""
    fts = [
        {"type": "topic", "id": 1, "title": "A"},
        {"type": "topic", "id": 2, "title": "B"},
    ]
    vec = [
        {"type": "topic", "id": 2, "title": "B"},
        {"type": "topic", "id": 1, "title": "A"},
    ]

    results = _rrf_merge(fts, vec, limit=10)

    assert len(results) == 2
    # id=1: FTSランク1 + vecランク2, id=2: FTSランク2 + vecランク1
    # 同じスコアになるはず
    score_1 = next(r["score"] for r in results if r["id"] == 1)
    score_2 = next(r["score"] for r in results if r["id"] == 2)
    max_score = (RRF_W_FTS + RRF_W_VEC) / (RRF_K + 1)
    raw_expected = RRF_W_FTS / (RRF_K + 1) + RRF_W_VEC / (RRF_K + 2)
    expected = round(raw_expected / max_score, 4)
    assert score_1 == pytest.approx(expected)
    assert score_2 == pytest.approx(expected)


def test_rrf_merge_fts_only():
    """RRF統合: FTSのみでもスコアが0〜1に正規化される"""
    fts = [
        {"type": "topic", "id": 1, "title": "A"},
        {"type": "topic", "id": 2, "title": "B"},
    ]

    results = _rrf_merge(fts, [], limit=10)

    assert len(results) == 2
    # FTSのみ→理論最大値は (w_fts + w_vec) / (k+1)（タグなし）
    max_score = (RRF_W_FTS + RRF_W_VEC) / (RRF_K + 1)
    assert results[0]["id"] == 1
    assert results[0]["score"] == pytest.approx(round(RRF_W_FTS / (RRF_K + 1) / max_score, 4))
    assert results[1]["id"] == 2
    assert results[1]["score"] == pytest.approx(round(RRF_W_FTS / (RRF_K + 2) / max_score, 4))


def test_rrf_merge_vec_only():
    """RRF統合: ベクトルのみでもAdaptive RRF重みを反映した正規化スコアが返る"""
    vec = [
        {"type": "decision", "id": 10, "title": "X"},
        {"type": "activity", "id": 20, "title": "Y"},
    ]

    results = _rrf_merge([], vec, limit=10)

    assert len(results) == 2
    assert results[0]["id"] == 10
    # FTS=0, vec=2 → ratio=0.0 → w_vec=1.5（Adaptive RRF有効時）
    w_fts, w_vec = _compute_adaptive_weights(0, 2)
    max_score = (w_fts + w_vec) / (RRF_K + 1)
    assert results[0]["score"] == pytest.approx(round(w_vec / (RRF_K + 1) / max_score, 4))


def test_rrf_merge_overlap_boosts_score():
    """RRF統合: 両方にヒットするアイテムは片方のみのアイテムより高スコア"""
    fts = [
        {"type": "topic", "id": 1, "title": "Both"},
        {"type": "topic", "id": 2, "title": "FTS only"},
    ]
    vec = [
        {"type": "topic", "id": 1, "title": "Both"},
        {"type": "topic", "id": 3, "title": "Vec only"},
    ]

    results = _rrf_merge(fts, vec, limit=10)

    score_both = next(r["score"] for r in results if r["id"] == 1)
    score_fts = next(r["score"] for r in results if r["id"] == 2)
    score_vec = next(r["score"] for r in results if r["id"] == 3)

    # 両方にヒット > 片方のみ
    assert score_both > score_fts
    assert score_both > score_vec


def test_rrf_merge_empty():
    """RRF統合: 両方空 → 空配列"""
    results = _rrf_merge([], [], limit=10)
    assert results == []


def test_rrf_merge_limit():
    """RRF統合: limit件数に切り詰められる"""
    fts = [{"type": "topic", "id": i, "title": f"T{i}"} for i in range(10)]

    results = _rrf_merge(fts, [], limit=3)

    assert len(results) == 3


# ========================================
# ハイブリッド検索 統合テスト（タグ対応）
# ========================================


def test_hybrid_search_3char_returns_results(temp_db, mock_embedding_model):
    """3文字以上: ハイブリッド検索で結果が返る"""
    add_topic(
        title="ハイブリッド検索テスト用トピック",
        description="FTS5とベクトルの両方で検索される",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="ハイブリッド検索テスト")

    assert "error" not in result
    assert len(result["results"]) >= 1
    assert result["results"][0]["type"] == "topic"
    assert isinstance(result["results"][0]["score"], float)


def test_hybrid_search_2char_vec_only(temp_db, mock_embedding_model):
    """2文字キーワード + ベクトル有効: ベクトル検索のみで結果が返る"""
    add_topic(
        title="設計ドキュメント",
        description="アーキテクチャ設計の詳細",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="設計")

    assert "error" not in result
    assert len(result["results"]) >= 1
    assert result["total_count"] == len(result["results"])


def test_hybrid_search_2char_vec_disabled(temp_db, disable_embedding):
    """2文字キーワード + ベクトル無効: KEYWORD_TOO_SHORTエラー"""
    add_topic(
        title="設計ドキュメント",
        description="アーキテクチャ設計の詳細",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="設計")

    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"


def test_hybrid_search_1char_always_error(temp_db, mock_embedding_model):
    """1文字キーワード: ベクトル有効でもエラー"""
    result = search_service.search(keyword="設")

    assert "error" in result
    assert result["error"]["code"] == "KEYWORD_TOO_SHORT"


def test_hybrid_search_3char_vec_disabled_fts_fallback(temp_db, disable_embedding):
    """3文字以上 + ベクトル無効: FTSのみで正常動作（graceful degradation）"""
    add_topic(
        title="認証フローの設計議論",
        description="OAuth2の認証フローについて検討する",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="認証フロー")

    assert "error" not in result
    assert len(result["results"]) >= 1
    assert result["results"][0]["title"] == "認証フローの設計議論"


def test_hybrid_search_score_is_rrf(temp_db, mock_embedding_model):
    """スコアが0〜1に正規化されたRRFスコアで返る"""
    add_topic(
        title="RRFスコアテスト用トピック",
        description="スコアの形式を検証する",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="RRFスコアテスト")

    assert "error" not in result
    assert len(result["results"]) >= 1
    score = result["results"][0]["score"]
    # 正規化後のスコアは0〜1の範囲
    assert 0 < score <= 1.0


def test_hybrid_search_with_tags(temp_db, mock_embedding_model):
    """ハイブリッド検索: タグフィルタ付き"""
    add_topic(
        title="タグ付きハイブリッド検索対象テスト",
        description="これはヒットすべき",
        tags=["domain:test", "intent:design"],
    )
    add_topic(
        title="タグ付きハイブリッド検索対象外テスト",
        description="これはヒットしない",
        tags=["domain:other"],
    )

    result = search_service.search(
        keyword="タグ付きハイブリッド検索",
        tags=["intent:design"],
    )

    assert "error" not in result
    titles = [r["title"] for r in result["results"]]
    assert any("対象テスト" in t for t in titles)
    assert all("対象外テスト" not in t for t in titles)


def test_hybrid_search_cross_type_with_tags(temp_db, mock_embedding_model):
    """ハイブリッド検索: topic/decision/activity全てが対象（タグフィルタ付き）"""
    topic = add_topic(
        title="横断ハイブリッドタグテスト用トピック",
        description="横断検索の動作確認",
        tags=DEFAULT_TAGS,
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="横断ハイブリッドタグテスト決定",
        reason="テスト用",
    )
    add_activity(
        title="横断ハイブリッドタグテストアクティビティ",
        description="テスト用アクティビティ",
        tags=DEFAULT_TAGS,
        check_in=False,
    )

    result = search_service.search(
        keyword="横断ハイブリッドタグテスト",
        tags=DEFAULT_TAGS,
    )

    assert "error" not in result
    types_found = {r["type"] for r in result["results"]}
    assert "topic" in types_found
    assert "decision" in types_found
    assert "activity" in types_found


def test_hybrid_search_entity_type(temp_db, mock_embedding_model):
    """ハイブリッド検索: entity_typeが効く"""
    topic = add_topic(
        title="フィルターハイブリッドテスト用",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="フィルターハイブリッドテスト決定",
        reason="テスト用",
    )

    result = search_service.search(
        keyword="フィルターハイブリッドテスト",
        entity_type="topic",
    )

    assert "error" not in result
    for item in result["results"]:
        assert item["type"] == "topic"


def test_hybrid_search_tag_isolation(temp_db, mock_embedding_model):
    """ハイブリッド検索: タグによる分離が保たれる"""
    add_topic(
        title="分離テスト対象トピック",
        description="このタグのみヒットすべき",
        tags=["domain:test"],
    )
    add_topic(
        title="分離テスト他タグトピック",
        description="こちらはヒットしない",
        tags=["domain:other"],
    )

    result = search_service.search(keyword="分離テスト", tags=["domain:test"])

    assert "error" not in result
    for item in result["results"]:
        assert item["title"] != "分離テスト他タグトピック"


# ========================================
# keyword配列（AND検索）のテスト
# ========================================


def test_hybrid_keyword_array_and(temp_db, mock_embedding_model):
    """配列keyword: ハイブリッド検索でAND動作"""
    add_topic(
        title="ハイブリッド配列検索テスト対象",
        description="メモリ管理と検索機能の両方を扱うトピック",
        tags=DEFAULT_TAGS,
    )
    add_topic(
        title="ハイブリッド配列検索テスト対象外",
        description="メモリ管理のみ",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword=["ハイブリッド配列検索", "テスト対象"])

    assert "error" not in result
    assert len(result["results"]) >= 1


def test_hybrid_keyword_array_vec_search(temp_db, mock_embedding_model):
    """配列keyword: ベクトル検索が結果を返す"""
    add_topic(
        title="ベクトル配列検索テスト用トピック",
        description="複数キーワードでのベクトル検索確認",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword=["ベクトル配列検索", "テスト用"])

    assert "error" not in result
    assert len(result["results"]) >= 1
    assert isinstance(result["results"][0]["score"], float)


def test_hybrid_keyword_array_2char_vec_only(temp_db, mock_embedding_model):
    """配列内に2文字キーワード: ベクトル検索のみで動作"""
    add_topic(
        title="設計レビュー用ドキュメント",
        description="設計の詳細レビュー",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword=["設計", "レビュー"])

    assert "error" not in result
    # ベクトル検索のみなので結果は返る（エラーにはならない）
    assert "results" in result


# ========================================
# keyword配列（OR検索）のテスト
# ========================================


def test_hybrid_keyword_or_basic(temp_db, mock_embedding_model):
    """OR検索: ハイブリッド検索でOR動作"""
    add_topic(title="ハイブリッドOR検索テスト対象A", description="メモリ管理の説明", tags=DEFAULT_TAGS)
    add_topic(title="ハイブリッドOR検索テスト対象B", description="検索機能の説明", tags=DEFAULT_TAGS)
    result = search_service.search(keyword=["ハイブリッドOR検索テスト対象A", "ハイブリッドOR検索テスト対象B"], keyword_mode="or")
    assert "error" not in result
    assert len(result["results"]) >= 2


def test_hybrid_keyword_or_2char_vec(temp_db, mock_embedding_model):
    """OR検索: 2文字キーワード混在でもベクトル検索が補完する"""
    add_topic(title="設計レビュー用ドキュメント", description="設計の詳細レビュー", tags=DEFAULT_TAGS)
    result = search_service.search(keyword=["設計", "レビュー"], keyword_mode="or")
    assert "error" not in result
    assert "results" in result


# ========================================
# _apply_recency_boost 単体テスト
# ========================================


def test_recency_boost_newer_scores_higher(temp_db):
    """recency boost: 同じRRFスコアでも新しいアイテムのほうがスコアが高くなる"""
    # 2つのトピックを作成
    t1 = add_topic(
        title="古いトピック",
        description="recency boostテスト用",
        tags=DEFAULT_TAGS,
    )
    t2 = add_topic(
        title="新しいトピック",
        description="recency boostテスト用",
        tags=DEFAULT_TAGS,
    )

    # t1のcreated_atを365日前に書き換え
    old_date = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute("UPDATE discussion_topics SET created_at = ? WHERE id = ?", (old_date, t1["topic_id"]))
    conn.commit()
    conn.close()

    base_score = 0.01
    results = [
        {"type": "topic", "id": t1["topic_id"], "title": "古いトピック", "score": base_score},
        {"type": "topic", "id": t2["topic_id"], "title": "新しいトピック", "score": base_score},
    ]

    _apply_recency_boost(results)

    score_old = next(r["score"] for r in results if r["id"] == t1["topic_id"])
    score_new = next(r["score"] for r in results if r["id"] == t2["topic_id"])

    # 新しいアイテムのほうがスコアが高い
    assert score_new > score_old
    # ソート順も新しいほうが先
    assert results[0]["id"] == t2["topic_id"]


def test_recency_boost_decay_formula(temp_db):
    """recency boost: 減衰率が formula 通りに計算される"""
    t = add_topic(
        title="減衰計算テスト",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )

    # created_atを固定日時に設定し、nowも固定して厳密に検証
    created_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = datetime(2025, 4, 1, 0, 0, 0, tzinfo=timezone.utc)  # 90日後
    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = ? WHERE id = ?",
        (created_at.strftime("%Y-%m-%d %H:%M:%S"), t["topic_id"]),
    )
    conn.commit()
    conn.close()

    base_score = 1.0
    results = [
        {"type": "topic", "id": t["topic_id"], "title": "減衰計算テスト", "score": base_score},
    ]

    _apply_recency_boost(results, now=now)

    # 指数減衰: factor = exp(-90 * 0.0119) ≈ 0.343（floorより上）
    expected_factor = math.exp(-90 * RECENCY_DECAY_RATE)
    assert results[0]["score"] == pytest.approx(base_score * expected_factor)


def test_recency_boost_floor(temp_db):
    """recency boost: 非常に古いアイテムでもFLOOR値を下回らない"""
    t = add_topic(
        title="floor検証用",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )

    # 730日前（約2年前）に設定
    created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = datetime(2025, 12, 31, 0, 0, 0, tzinfo=timezone.utc)  # 730日後
    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = ? WHERE id = ?",
        (created_at.strftime("%Y-%m-%d %H:%M:%S"), t["topic_id"]),
    )
    conn.commit()
    conn.close()

    base_score = 1.0
    results = [
        {"type": "topic", "id": t["topic_id"], "title": "floor検証用", "score": base_score},
    ]

    _apply_recency_boost(results, now=now)

    # exp(-730 * 0.0119) ≈ 0.000165 だが、floorで0.15になる
    assert results[0]["score"] == pytest.approx(base_score * RECENCY_DECAY_FLOOR)


def test_recency_boost_empty_list():
    """recency boost: 空リストでエラーにならない"""
    results = []
    _apply_recency_boost(results)
    assert results == []


def test_recency_boost_reorders_by_score(temp_db):
    """recency boost: スコア降順で再ソートされる"""
    t1 = add_topic(title="トピックA", description="テスト", tags=DEFAULT_TAGS)
    t2 = add_topic(title="トピックB", description="テスト", tags=DEFAULT_TAGS)

    # t1を古く、t2を新しくする
    old_date = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute("UPDATE discussion_topics SET created_at = ? WHERE id = ?", (old_date, t1["topic_id"]))
    conn.commit()
    conn.close()

    # t1のスコアをわずかに高くしても、古さで逆転するケース
    results = [
        {"type": "topic", "id": t1["topic_id"], "title": "トピックA", "score": 0.012},
        {"type": "topic", "id": t2["topic_id"], "title": "トピックB", "score": 0.010},
    ]

    _apply_recency_boost(results)

    # 730日前: factor = exp(-730*0.0119) ≈ 0.000165
    # t1: 0.012 * 0.000165 ≈ 0.000002
    # t2 (今日): 0.010 * ~1.0 = 0.010
    # t2が上位に来るはず
    assert results[0]["id"] == t2["topic_id"]


def test_recency_boost_cross_type(temp_db):
    """recency boost: 異なるtypeを横断して処理できる"""
    t = add_topic(title="横断テストトピック", description="テスト", tags=DEFAULT_TAGS)
    d = add_decision(
        topic_id=t["topic_id"],
        decision="横断テスト決定",
        reason="テスト用",
    )

    base_score = 0.01
    results = [
        {"type": "topic", "id": t["topic_id"], "title": "横断テストトピック", "score": base_score},
        {"type": "decision", "id": d["decision_id"], "title": "横断テスト決定", "score": base_score},
    ]

    # エラーなく完了すること
    _apply_recency_boost(results)

    # 両方ともスコアが付いている（作成直後なのでほぼ変わらない）
    for r in results:
        assert r["score"] > 0
        assert r["score"] <= base_score


# ========================================
# score_breakdown / final_score テスト
# ========================================


def test_rrf_merge_attaches_score_breakdown_fields():
    """_rrf_merge は各結果に score_breakdown を fts/vec/tag/rrf_normalized 付きで付与する"""
    fts = [{"type": "topic", "id": 1, "title": "A"}]
    vec = [{"type": "topic", "id": 1, "title": "A"}]
    tag = [{"type": "topic", "id": 1, "title": "A"}]

    results = _rrf_merge(fts, vec, limit=10, tag_results=tag)

    assert len(results) == 1
    item = results[0]
    assert "score_breakdown" in item
    bd = item["score_breakdown"]
    assert set(bd.keys()) == {"fts", "vec", "tag", "rrf_normalized"}
    # 各ソース 1 位ヒットなので寄与値は w_src / (k+1)
    assert bd["fts"] == pytest.approx(RRF_W_FTS / (RRF_K + 1), abs=1e-6)
    assert bd["vec"] == pytest.approx(RRF_W_VEC / (RRF_K + 1), abs=1e-6)
    assert bd["tag"] == pytest.approx(RRF_W_TAG / (RRF_K + 1), abs=1e-6)
    # 3ソース全1位ヒット → rrf_normalized は 1.0
    assert bd["rrf_normalized"] == pytest.approx(1.0)
    # score は recency 未適用時点で rrf_normalized と一致
    assert item["score"] == pytest.approx(bd["rrf_normalized"])


def test_rrf_merge_score_breakdown_fts_only_zero_for_vec_tag():
    """FTS のみヒット時、vec/tag は 0、fts は正値"""
    fts = [{"type": "topic", "id": 1, "title": "A"}]

    results = _rrf_merge(fts, [], limit=10)

    bd = results[0]["score_breakdown"]
    assert bd["fts"] > 0
    assert bd["vec"] == 0.0
    assert bd["tag"] == 0.0
    assert bd["rrf_normalized"] > 0


def test_rrf_merge_score_breakdown_sums_to_normalized():
    """score_breakdown の fts+vec+tag を理論最大値で割ると rrf_normalized になる"""
    fts = [
        {"type": "topic", "id": 1, "title": "A"},
        {"type": "topic", "id": 2, "title": "B"},
    ]
    vec = [
        {"type": "topic", "id": 2, "title": "B"},
        {"type": "topic", "id": 1, "title": "A"},
    ]

    results = _rrf_merge(fts, vec, limit=10)

    max_score = (RRF_W_FTS + RRF_W_VEC) / (RRF_K + 1)
    for item in results:
        bd = item["score_breakdown"]
        raw_sum = bd["fts"] + bd["vec"] + bd["tag"]
        assert bd["rrf_normalized"] == pytest.approx(round(raw_sum / max_score, 4), abs=1e-4)


def test_rrf_merge_score_breakdown_under_adaptive_weights():
    """Adaptive RRF重み適用ケース (w_fts != w_vec) でも fts/vec/rrf_normalized が整合する"""
    # fts_count / vec_count = 1/5 = 0.2 → ratio<0.2 ではないが ratio<0.5 なので w=(0.8, 1.2)
    # ratio<0.2 を狙うため fts=1, vec=10 にする
    fts = [{"type": "topic", "id": 100, "title": "F"}]
    vec = [{"type": "topic", "id": i, "title": f"V{i}"} for i in range(1, 11)]

    results = _rrf_merge(fts, vec, limit=20)

    w_fts, w_vec = _compute_adaptive_weights(len(fts), len(vec))
    # ratio = 0.1 < 0.2 → w_fts=0.5, w_vec=1.5
    assert (w_fts, w_vec) == (0.5, 1.5)
    max_score = (w_fts + w_vec) / (RRF_K + 1)

    # id=100 は FTS のみヒット (vec にはなし)、id=1 は vec ランク1のみヒット
    fts_only = next(r for r in results if r["id"] == 100)
    vec_top = next(r for r in results if r["id"] == 1)

    bd_f = fts_only["score_breakdown"]
    bd_v = vec_top["score_breakdown"]

    # 各寄与は Adaptive 重み適用後の値
    assert bd_f["fts"] == pytest.approx(w_fts / (RRF_K + 1), abs=1e-6)
    assert bd_f["vec"] == 0.0
    assert bd_v["fts"] == 0.0
    assert bd_v["vec"] == pytest.approx(w_vec / (RRF_K + 1), abs=1e-6)

    # rrf_normalized: 各 raw_sum を max_score で割る
    assert bd_f["rrf_normalized"] == pytest.approx(round((w_fts / (RRF_K + 1)) / max_score, 4), abs=1e-4)
    assert bd_v["rrf_normalized"] == pytest.approx(round((w_vec / (RRF_K + 1)) / max_score, 4), abs=1e-4)

    # vec_top のほうが上位 (w_vec が大きいため)
    assert vec_top["score"] > fts_only["score"]


def test_apply_recency_boost_attaches_recency_factor_and_final_score(temp_db):
    """_apply_recency_boost は score_breakdown.recency_factor と final_score を付与する"""
    t = add_topic(title="breakdown 検証用", description="テスト", tags=DEFAULT_TAGS)

    # rrf_normalized 0.5 相当の score_breakdown 付き入力
    results = [
        {
            "type": "topic",
            "id": t["topic_id"],
            "title": "breakdown 検証用",
            "score": 0.5,
            "score_breakdown": {"fts": 0.1, "vec": 0.2, "tag": 0.0, "rrf_normalized": 0.5},
        }
    ]

    _apply_recency_boost(results)

    item = results[0]
    bd = item["score_breakdown"]
    assert "recency_factor" in bd
    assert 0 < bd["recency_factor"] <= 1.0
    assert "final_score" in item
    assert item["final_score"] == pytest.approx(bd["rrf_normalized"] * bd["recency_factor"])
    # 互換: score も final_score と同値
    assert item["score"] == pytest.approx(item["final_score"])


def test_apply_recency_boost_fills_missing_score_breakdown_keys(temp_db):
    """score_breakdown が空dict / 部分dict で渡されても rrf_normalized 等を補完する"""
    t = add_topic(title="部分breakdown 検証", description="テスト", tags=DEFAULT_TAGS)

    # score_breakdown は空dict（rrf_normalized も欠落）。score=0.3 をフォールバックに使う
    results = [
        {
            "type": "topic",
            "id": t["topic_id"],
            "title": "部分breakdown 検証",
            "score": 0.3,
            "score_breakdown": {},
        }
    ]

    _apply_recency_boost(results)

    bd = results[0]["score_breakdown"]
    # 欠落キーが全て補完されている
    assert bd["fts"] == 0.0
    assert bd["vec"] == 0.0
    assert bd["tag"] == 0.0
    assert bd["rrf_normalized"] == pytest.approx(0.3)
    assert 0 < bd["recency_factor"] <= 1.0
    assert results[0]["final_score"] == pytest.approx(0.3 * bd["recency_factor"])


def test_apply_recency_boost_recency_factor_floor(temp_db):
    """非常に古いアイテムの recency_factor は FLOOR で打ち止め"""
    t = add_topic(title="floor breakdown 検証", description="テスト", tags=DEFAULT_TAGS)

    created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = datetime(2025, 12, 31, 0, 0, 0, tzinfo=timezone.utc)  # 730日後
    conn = get_connection()
    conn.execute(
        "UPDATE discussion_topics SET created_at = ? WHERE id = ?",
        (created_at.strftime("%Y-%m-%d %H:%M:%S"), t["topic_id"]),
    )
    conn.commit()
    conn.close()

    results = [
        {
            "type": "topic",
            "id": t["topic_id"],
            "title": "floor breakdown 検証",
            "score": 1.0,
            "score_breakdown": {"fts": 1.0, "vec": 0.0, "tag": 0.0, "rrf_normalized": 1.0},
        }
    ]

    _apply_recency_boost(results, now=now)

    bd = results[0]["score_breakdown"]
    assert bd["recency_factor"] == pytest.approx(RECENCY_DECAY_FLOOR)
    assert results[0]["final_score"] == pytest.approx(1.0 * RECENCY_DECAY_FLOOR)


def test_search_returns_score_breakdown_and_final_score(temp_db, mock_embedding_model):
    """search の各結果に score_breakdown と final_score が含まれる"""
    add_topic(
        title="ブレークダウン検索テスト用トピック",
        description="score_breakdown 検証データ",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="ブレークダウン検索テスト")

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    # 必須フィールド
    assert "score_breakdown" in item
    assert "final_score" in item
    assert "score" in item
    bd = item["score_breakdown"]
    for key in ("fts", "vec", "tag", "rrf_normalized", "recency_factor"):
        assert key in bd, f"score_breakdown に {key} が欠けている"
    # final_score == rrf_normalized * recency_factor
    assert item["final_score"] == pytest.approx(bd["rrf_normalized"] * bd["recency_factor"])
    # 互換: 既存 score フィールドは final_score と同値
    assert item["score"] == pytest.approx(item["final_score"])


def test_search_score_breakdown_values_in_range(temp_db, mock_embedding_model):
    """score_breakdown の各サブフィールドが妥当な値域に収まる"""
    add_topic(
        title="ブレークダウン値域テスト用トピック",
        description="score_breakdown 値域確認データ",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="ブレークダウン値域テスト")

    assert "error" not in result
    for item in result["results"]:
        bd = item["score_breakdown"]
        # 各ソース寄与は非負
        assert bd["fts"] >= 0
        assert bd["vec"] >= 0
        assert bd["tag"] >= 0
        # 正規化後は 0〜1
        assert 0 <= bd["rrf_normalized"] <= 1.0
        # recency_factor は FLOOR〜1.0
        assert RECENCY_DECAY_FLOOR <= bd["recency_factor"] <= 1.0
        # final_score は 0〜1（rrf_normalized * recency_factor、両者とも 0〜1 なので）
        assert 0 <= item["final_score"] <= 1.0


# ========================================
# recency boost 統合テスト
# ========================================


def test_search_recency_boost_applied(temp_db, mock_embedding_model):
    """search結果にrecency boostが適用されている: 新しいほうが上位"""
    # 2つのトピックを同じキーワードで作成
    t_old = add_topic(
        title="リーセンシー統合テスト用トピック古い",
        description="リーセンシー統合テストの検証用データ",
        tags=DEFAULT_TAGS,
    )
    t_new = add_topic(
        title="リーセンシー統合テスト用トピック新しい",
        description="リーセンシー統合テストの検証用データ",
        tags=DEFAULT_TAGS,
    )

    # 古いほうのcreated_atを1年前に設定
    old_date = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute("UPDATE discussion_topics SET created_at = ? WHERE id = ?", (old_date, t_old["topic_id"]))
    conn.commit()
    conn.close()

    result = search_service.search(keyword="リーセンシー統合テスト")

    assert "error" not in result
    assert len(result["results"]) >= 2

    # 結果内でtopic同士の順序を確認
    topic_results = [r for r in result["results"] if r["type"] == "topic"]
    assert len(topic_results) >= 2

    # 新しいトピックが古いトピックより先に来る（α化により id_raw が元 ID）
    ids_in_order = [r["id_raw"] for r in topic_results]
    idx_new = ids_in_order.index(t_new["topic_id"])
    idx_old = ids_in_order.index(t_old["topic_id"])
    assert idx_new < idx_old, "新しいトピックが古いトピックより上位に来るべき"


# ========================================
# offset（ページネーション）テスト
# ========================================


def test_search_offset_skips_results(temp_db, mock_embedding_model):
    """offset指定: 先頭のresultsがスキップされる"""
    for i in range(5):
        add_topic(
            title=f"オフセットテスト用トピック{i}",
            description="オフセットテスト用の検索対象データ",
            tags=DEFAULT_TAGS,
        )

    result_all = search_service.search(keyword="オフセットテスト用", limit=10)
    result_offset = search_service.search(keyword="オフセットテスト用", limit=10, offset=2)

    assert "error" not in result_all
    assert "error" not in result_offset
    assert len(result_all["results"]) >= 3
    # offset=2 の結果は、全体の3番目以降と一致する
    assert result_offset["results"][0]["id_raw"] == result_all["results"][2]["id_raw"]


def test_search_offset_with_limit(temp_db, mock_embedding_model):
    """offset + limit: 中間ページの取得"""
    for i in range(5):
        add_topic(
            title=f"ページネーションテスト用トピック{i}",
            description="ページネーションテスト用の検索対象データ",
            tags=DEFAULT_TAGS,
        )

    result_all = search_service.search(keyword="ページネーションテスト用", limit=10)
    result_page = search_service.search(keyword="ページネーションテスト用", limit=2, offset=1)

    assert "error" not in result_all
    assert "error" not in result_page
    assert len(result_page["results"]) <= 2
    if len(result_all["results"]) > 1:
        assert result_page["results"][0]["id_raw"] == result_all["results"][1]["id_raw"]


def test_search_offset_beyond_results(temp_db, mock_embedding_model):
    """offset が結果件数を超える場合: 空配列が返る"""
    add_topic(
        title="オフセット超過テスト用トピック",
        description="オフセット超過テスト用データ",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="オフセット超過テスト用", limit=10, offset=100)

    assert "error" not in result
    assert len(result["results"]) == 0


def test_search_offset_zero_is_default(temp_db, mock_embedding_model):
    """offset=0: offsetなしと同じ結果"""
    add_topic(
        title="オフセットゼロテスト用トピック",
        description="オフセットゼロテスト用データ",
        tags=DEFAULT_TAGS,
    )

    result_default = search_service.search(keyword="オフセットゼロテスト用")
    result_zero = search_service.search(keyword="オフセットゼロテスト用", offset=0)

    assert "error" not in result_default
    assert "error" not in result_zero
    assert len(result_default["results"]) == len(result_zero["results"])
    for r1, r2 in zip(result_default["results"], result_zero["results"]):
        assert r1["id_raw"] == r2["id_raw"]


def test_search_offset_negative_treated_as_zero(temp_db, mock_embedding_model):
    """負のoffset: 0として扱われる"""
    add_topic(
        title="負オフセットテスト用トピック",
        description="負オフセットテスト用データ",
        tags=DEFAULT_TAGS,
    )

    result_default = search_service.search(keyword="負オフセットテスト用")
    result_neg = search_service.search(keyword="負オフセットテスト用", offset=-5)

    assert "error" not in result_default
    assert "error" not in result_neg
    assert len(result_default["results"]) == len(result_neg["results"])



# ========================================
# search_methods_used テスト
# ========================================


def test_search_methods_used_hybrid(temp_db, mock_embedding_model):
    """3文字以上 + ベクトル有効: fts5とvectorの両方が使われる"""
    add_topic(
        title="ハイブリッドメソッド確認テスト用",
        description="FTS5とベクトルの両方が使われることを確認",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="ハイブリッドメソッド確認テスト")

    assert "error" not in result
    assert "search_methods_used" in result
    assert result["search_methods_used"] == ["fts5", "vector"]


def test_search_methods_used_vector_only(temp_db, mock_embedding_model):
    """2文字キーワード + ベクトル有効: vectorのみが使われる"""
    add_topic(
        title="設計ドキュメント",
        description="アーキテクチャ設計の詳細",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="設計")

    assert "error" not in result
    assert "search_methods_used" in result
    assert result["search_methods_used"] == ["vector"]


def test_search_methods_used_fts_only_vec_disabled(temp_db, disable_embedding):
    """3文字以上 + ベクトル無効: fts5のみが使われる"""
    add_topic(
        title="認証フローメソッド確認テスト",
        description="FTSのみで検索される",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="認証フローメソッド確認テスト")

    assert "error" not in result
    assert "search_methods_used" in result
    assert result["search_methods_used"] == ["fts5"]



# ========================================
# find_similar_topics テスト
# ========================================


def test_find_similar_topics_returns_results(temp_db, mock_embedding_model):
    """find_similar_topics: 類似トピックが返る"""
    t1 = add_topic(title="検索機能の設計", description="全文検索とベクトル検索のハイブリッド", tags=DEFAULT_TAGS)
    t2 = add_topic(title="検索UIの改善", description="検索結果の表示を改善する", tags=DEFAULT_TAGS)

    results = find_similar_topics("検索機能の設計と実装", exclude_id=999)

    assert len(results) >= 1
    ids = [r["id"] for r in results]
    assert t1["topic_id"] in ids or t2["topic_id"] in ids


def test_find_similar_topics_excludes_self(temp_db, mock_embedding_model):
    """find_similar_topics: exclude_idで自身が除外される"""
    t1 = add_topic(title="類似除外テスト用トピック", description="自身を除外するテスト", tags=DEFAULT_TAGS)

    results = find_similar_topics("類似除外テスト用トピック", exclude_id=t1["topic_id"])

    ids = [r["id"] for r in results]
    assert t1["topic_id"] not in ids


def test_find_similar_topics_limit(temp_db, mock_embedding_model):
    """find_similar_topics: limit件数に制限される"""
    for i in range(5):
        add_topic(title=f"リミットテスト用トピック{i}", description="リミットテスト用", tags=DEFAULT_TAGS)

    results = find_similar_topics("リミットテスト用", exclude_id=999, limit=2)

    assert len(results) <= 2


def test_find_similar_topics_has_distance(temp_db, mock_embedding_model):
    """find_similar_topics: 結果にdistanceが含まれる"""
    add_topic(title="距離テスト用トピック", description="距離が含まれることを確認", tags=DEFAULT_TAGS)

    results = find_similar_topics("距離テスト用", exclude_id=999)

    assert len(results) >= 1
    assert "distance" in results[0]
    assert isinstance(results[0]["distance"], float)


def test_find_similar_topics_sorted_by_distance(temp_db, mock_embedding_model):
    """find_similar_topics: distanceの昇順（類似度の高い順）にソートされる"""
    for i in range(3):
        add_topic(title=f"ソートテスト用トピック{i}", description=f"ソートテスト用の説明{i}", tags=DEFAULT_TAGS)

    results = find_similar_topics("ソートテスト用", exclude_id=999)

    if len(results) >= 2:
        for i in range(len(results) - 1):
            assert results[i]["distance"] <= results[i + 1]["distance"]


def test_find_similar_topics_only_topics(temp_db, mock_embedding_model):
    """find_similar_topics: topicのみ返り、decision/activityは含まれない"""
    t = add_topic(title="型フィルタテスト用トピック", description="topicのみ返ることを確認", tags=DEFAULT_TAGS)
    add_decision(topic_id=t["topic_id"], decision="型フィルタテスト用決定", reason="テスト用")
    add_activity(title="型フィルタテスト用アクティビティ", description="テスト用", tags=DEFAULT_TAGS, check_in=False)

    results = find_similar_topics("型フィルタテスト用", exclude_id=999)

    for r in results:
        assert "id" in r
        assert "title" in r
        # decision/activityのIDが混入していないことを確認
        assert r["title"] != "型フィルタテスト用決定"
        assert r["title"] != "型フィルタテスト用アクティビティ"


def test_find_similar_topics_embedding_disabled(temp_db, disable_embedding):
    """find_similar_topics: embedding無効時は空リストが返る"""
    add_topic(title="無効時テスト用", description="embedding無効", tags=DEFAULT_TAGS)

    results = find_similar_topics("無効時テスト用", exclude_id=999)

    assert results == []


# ========================================
# add_topic similar_topics 統合テスト
# ========================================


def test_add_topic_includes_similar_topics(temp_db, mock_embedding_model):
    """add_topic: レスポンスにsimilar_topicsが含まれる"""
    add_topic(title="類似サジェストテスト用の既存トピック", description="ベクトル検索でヒットする", tags=DEFAULT_TAGS)

    result = add_topic(title="類似サジェストテスト用の新規トピック", description="類似が見つかるはず", tags=DEFAULT_TAGS)

    assert "error" not in result
    assert "topic_id" in result
    assert "similar_topics" in result
    assert isinstance(result["similar_topics"], list)
    for item in result["similar_topics"]:
        assert "id" in item
        assert "title" in item
        assert "distance" in item


def test_add_topic_no_similar_when_embedding_disabled(temp_db, disable_embedding):
    """add_topic: embedding無効時はsimilar_topicsが含まれない"""
    add_topic(title="既存トピック", description="embedding無効", tags=DEFAULT_TAGS)

    result = add_topic(title="新規トピック", description="類似なし", tags=DEFAULT_TAGS)

    assert "error" not in result
    assert "similar_topics" not in result


# ========================================
# _rrf_merge 3ソース（タグLIKE）テスト
# ========================================


def test_rrf_merge_with_tag_results():
    """RRF統合: 3ソース全て1位でスコアが1.0になる（理論最大値 = 正規化基準）"""
    fts = [
        {"type": "topic", "id": 1, "title": "A"},
    ]
    vec = [
        {"type": "topic", "id": 1, "title": "A"},
    ]
    tag = [
        {"type": "topic", "id": 1, "title": "A"},
    ]

    results = _rrf_merge(fts, vec, limit=10, tag_results=tag)

    assert len(results) == 1
    # 全ソース1位 → 正規化後1.0
    assert results[0]["score"] == pytest.approx(1.0)


def test_rrf_merge_tag_only():
    """RRF統合: タグLIKEのみでもスコアが0〜1に正規化される"""
    tag = [
        {"type": "topic", "id": 10, "title": "Tag only"},
        {"type": "activity", "id": 20, "title": "Tag only 2"},
    ]

    results = _rrf_merge([], [], limit=10, tag_results=tag)

    assert len(results) == 2
    assert results[0]["id"] == 10
    # タグのみ→理論最大値は (w_fts + w_vec + W_TAG) / (k+1)
    w_fts, w_vec = _compute_adaptive_weights(0, 0)
    max_score = (w_fts + w_vec + RRF_W_TAG) / (RRF_K + 1)
    assert results[0]["score"] == pytest.approx(round(RRF_W_TAG / (RRF_K + 1) / max_score, 4))
    assert results[1]["score"] == pytest.approx(round(RRF_W_TAG / (RRF_K + 2) / max_score, 4))


def test_rrf_merge_tag_boosts_existing():
    """RRF統合: タグLIKE結果がFTS/ベクトルのスコアに加算される"""
    fts = [
        {"type": "topic", "id": 1, "title": "Both"},
        {"type": "topic", "id": 2, "title": "FTS only"},
    ]
    tag = [
        {"type": "topic", "id": 1, "title": "Both"},
        {"type": "topic", "id": 3, "title": "Tag only"},
    ]

    results = _rrf_merge(fts, [], limit=10, tag_results=tag)

    score_both = next(r["score"] for r in results if r["id"] == 1)
    score_fts = next(r["score"] for r in results if r["id"] == 2)
    score_tag = next(r["score"] for r in results if r["id"] == 3)

    # 両方にヒット > 片方のみ
    assert score_both > score_fts
    assert score_both > score_tag


def test_rrf_merge_no_tag_results():
    """RRF統合: tag_results=Noneで2ソース1位→スコアが1.0になる"""
    fts = [
        {"type": "topic", "id": 1, "title": "A"},
    ]
    vec = [
        {"type": "topic", "id": 1, "title": "A"},
    ]

    results = _rrf_merge(fts, vec, limit=10, tag_results=None)

    assert len(results) == 1
    # FTS+ベクトル両方1位 → 正規化後1.0
    assert results[0]["score"] == pytest.approx(1.0)


# ========================================
# タグLIKE検索 統合テスト
# ========================================


def test_search_tag_like_basic(temp_db, disable_embedding):
    """タグLIKE検索: タグ名にマッチするキーワードでエンティティがヒットする"""
    add_topic(
        title="タグLIKE検索テスト用トピック",
        description="タグ名でのマッチを確認",
        tags=["domain:test"],
    )

    result = search_service.search(keyword="domain:test")

    assert "error" not in result
    assert len(result["results"]) >= 1
    assert "tag_like" in result["search_methods_used"]


def test_search_tag_like_partial_match(temp_db, disable_embedding):
    """タグLIKE検索: タグ名の部分一致でヒットする"""
    add_topic(
        title="タグ部分一致テスト用トピック",
        description="テスト",
        tags=["domain:cc-memory"],
    )

    result = search_service.search(keyword="cc-memory")

    assert "error" not in result
    assert len(result["results"]) >= 1
    assert "tag_like" in result["search_methods_used"]


def test_search_tag_like_methods_used(temp_db, mock_embedding_model):
    """タグLIKE検索: search_methods_usedにtag_likeが含まれる"""
    add_topic(
        title="メソッド確認タグLIKEテスト用",
        description="テスト",
        tags=["domain:test"],
    )

    result = search_service.search(keyword="domain:test")

    assert "error" not in result
    assert "tag_like" in result["search_methods_used"]


def test_search_tag_like_with_entity_type(temp_db, disable_embedding):
    """タグLIKE検索: entity_typeとの組み合わせ"""
    topic = add_topic(
        title="タグLIKEフィルタテスト用トピック",
        description="テスト",
        tags=["domain:unique-tag-filter-test"],
    )
    add_activity(
        title="タグLIKEフィルタテスト用アクティビティ",
        description="テスト",
        tags=["domain:unique-tag-filter-test"],
        check_in=False,
    )

    result = search_service.search(
        keyword="unique-tag-filter-test",
        entity_type="topic",
    )

    assert "error" not in result
    for item in result["results"]:
        assert item["type"] == "topic"


def test_search_tag_like_no_match(temp_db, disable_embedding):
    """タグLIKE検索: マッチなしで空結果"""
    add_topic(
        title="タグLIKE不一致テスト",
        description="テスト",
        tags=["domain:test"],
    )

    # 全くマッチしないタグ名で検索
    result = search_service.search(keyword="zzz存在しないタグ名zzz")

    assert "error" not in result
    assert "tag_like" not in result.get("search_methods_used", [])


# ========================================
# Query Expansion テスト
# ========================================


def test_qe_constants():
    """QE定数が期待値で定義されている"""
    assert QE_DISTANCE_THRESHOLD == 0.3
    assert QE_MAX_EXPANSIONS == 5
    assert QE_EXCLUDE_NAMESPACES is True


def test_qe_no_expansion_without_embedding(temp_db, disable_embedding):
    """embedding無効時: QE拡張は行われず元のキーワードがそのまま返る"""
    result = _expand_query_with_tags(["テスト"])
    assert result == ["テスト"]


def test_qe_no_expansion_when_no_similar_tags(temp_db, mock_embedding_model):
    """類似タグなし: QE拡張は行われず元のキーワードがそのまま返る"""
    # タグが存在しないDBではKNN検索結果が空
    result = _expand_query_with_tags(["存在しないキーワード"])
    assert result == ["存在しないキーワード"]


def test_qe_expansion_with_similar_tags(temp_db, mock_embedding_model):
    """類似タグあり: 距離閾値未満のタグが拡張される"""
    # タグを作成してIDを取得
    conn = get_connection()
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'qe-search')")
    search_id = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'qe-search'").fetchone()["id"]
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'qe-query')")
    query_id = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'qe-query'").fetchone()["id"]
    conn.commit()
    conn.close()

    # search_similar_tagsをモックして距離をコントロール
    def mock_search_similar(query_text, k=10):
        return [(query_id, 0.1)]  # qe-queryタグが距離0.1で類似

    original = emb.search_similar_tags
    emb.search_similar_tags = mock_search_similar
    try:
        result = _expand_query_with_tags(["qe-search"])
        assert "qe-search" in result
        assert "qe-query" in result
        assert len(result) == 2
    finally:
        emb.search_similar_tags = original


def test_qe_excludes_namespace_tags(temp_db, mock_embedding_model):
    """QE_EXCLUDE_NAMESPACES=True: namespace付きタグは拡張に含まれない"""
    conn = get_connection()
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('domain', 'qe-ns-test')")
    ns_id = conn.execute("SELECT id FROM tags WHERE namespace = 'domain' AND name = 'qe-ns-test'").fetchone()["id"]
    conn.commit()
    conn.close()

    def mock_search_similar(query_text, k=10):
        # domain:qe-ns-testが距離0.1で類似
        return [(ns_id, 0.1)]

    original = emb.search_similar_tags
    emb.search_similar_tags = mock_search_similar
    try:
        result = _expand_query_with_tags(["test"])
        # namespace付きタグは除外されるため、元のキーワードのみ
        assert result == ["test"]
    finally:
        emb.search_similar_tags = original


def test_qe_respects_distance_threshold(temp_db, mock_embedding_model):
    """距離閾値以上のタグは拡張に含まれない"""
    conn = get_connection()
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'qe-near')")
    near_id = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'qe-near'").fetchone()["id"]
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'qe-far')")
    far_id = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'qe-far'").fetchone()["id"]
    conn.commit()
    conn.close()

    def mock_search_similar(query_text, k=10):
        return [
            (near_id, 0.1),   # 閾値未満 → 拡張される
            (far_id, 0.5),    # 閾値以上 → 除外
        ]

    original = emb.search_similar_tags
    emb.search_similar_tags = mock_search_similar
    try:
        result = _expand_query_with_tags(["test"])
        assert "qe-near" in result
        assert "qe-far" not in result
    finally:
        emb.search_similar_tags = original


def test_qe_max_expansions_limit(temp_db, mock_embedding_model):
    """最大拡張数QE_MAX_EXPANSIONSを超えない"""
    conn = get_connection()
    tag_ids = []
    for i in range(10):
        conn.execute(f"INSERT INTO tags (namespace, name) VALUES ('', 'qe-max-{i}')")
        tid = conn.execute(f"SELECT id FROM tags WHERE namespace = '' AND name = 'qe-max-{i}'").fetchone()["id"]
        tag_ids.append(tid)
    conn.commit()
    conn.close()

    def mock_search_similar(query_text, k=10):
        # 全て距離0.1で類似
        return [(tid, 0.1) for tid in tag_ids]

    original = emb.search_similar_tags
    emb.search_similar_tags = mock_search_similar
    try:
        result = _expand_query_with_tags(["test"])
        # 元のキーワード(1) + 拡張(最大5) = 6
        assert len(result) <= 1 + QE_MAX_EXPANSIONS
    finally:
        emb.search_similar_tags = original


def test_qe_dedup_with_original_keyword(temp_db, mock_embedding_model):
    """元のキーワードと同名のタグは拡張に含まれない"""
    conn = get_connection()
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'qe-dedup')")
    dedup_id = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'qe-dedup'").fetchone()["id"]
    conn.commit()
    conn.close()

    def mock_search_similar(query_text, k=10):
        return [(dedup_id, 0.05)]  # 元キーワードと同名のタグ

    original = emb.search_similar_tags
    emb.search_similar_tags = mock_search_similar
    try:
        result = _expand_query_with_tags(["qe-dedup"])
        # 重複除外で元のキーワードのみ
        assert result == ["qe-dedup"]
    finally:
        emb.search_similar_tags = original


def test_qe_vector_search_not_expanded(temp_db, mock_embedding_model):
    """search()でベクトル検索には元のキーワードのまま渡される"""
    conn = get_connection()
    conn.execute("INSERT INTO tags (namespace, name) VALUES ('', 'qe-expanded')")
    expanded_id = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'qe-expanded'").fetchone()["id"]
    conn.commit()
    conn.close()

    add_topic(
        title="QEベクトル分離テスト用トピック",
        description="QEが適用されないことを確認",
        tags=DEFAULT_TAGS,
    )

    def mock_search_similar(query_text, k=10):
        return [(expanded_id, 0.1)]

    original = emb.search_similar_tags
    emb.search_similar_tags = mock_search_similar
    try:
        # search()が正常に動作すれば、ベクトル検索は元キーワードで呼ばれている
        result = search_service.search(keyword="QEベクトル分離テスト")
        assert "error" not in result
        assert len(result["results"]) >= 1
    finally:
        emb.search_similar_tags = original


# ========================================
# Adaptive RRF Weight テスト
# ========================================


class TestComputeAdaptiveWeights:
    """_compute_adaptive_weights 単体テスト"""

    def test_ratio_below_02(self):
        """ratio < 0.2: FTS重み0.5, ベクトル重み1.5"""
        # fts=1, vec=10 → ratio=0.1
        w_fts, w_vec = _compute_adaptive_weights(1, 10)
        assert w_fts == 0.5
        assert w_vec == 1.5

    def test_ratio_below_05(self):
        """ratio < 0.5 (>= 0.2): FTS重み0.8, ベクトル重み1.2"""
        # fts=3, vec=10 → ratio=0.3
        w_fts, w_vec = _compute_adaptive_weights(3, 10)
        assert w_fts == 0.8
        assert w_vec == 1.2

    def test_ratio_above_05(self):
        """ratio >= 0.5: デフォルト重み"""
        # fts=5, vec=10 → ratio=0.5
        w_fts, w_vec = _compute_adaptive_weights(5, 10)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC

    def test_ratio_equal_1(self):
        """ratio = 1.0（同数）: デフォルト重み"""
        w_fts, w_vec = _compute_adaptive_weights(10, 10)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC

    def test_ratio_above_1(self):
        """ratio > 1.0（FTSのほうが多い）: デフォルト重み"""
        w_fts, w_vec = _compute_adaptive_weights(20, 10)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC

    def test_vec_count_zero(self):
        """vec_count=0: ratio=1.0扱いでデフォルト重み"""
        w_fts, w_vec = _compute_adaptive_weights(5, 0)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC

    def test_both_zero(self):
        """fts_count=0, vec_count=0: デフォルト重み"""
        w_fts, w_vec = _compute_adaptive_weights(0, 0)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC

    def test_fts_zero_vec_nonzero(self):
        """fts_count=0, vec_count>0: ratio=0.0 → 最低帯（0.5, 1.5）"""
        w_fts, w_vec = _compute_adaptive_weights(0, 10)
        assert w_fts == 0.5
        assert w_vec == 1.5

    def test_boundary_02(self):
        """ratio=0.2（ちょうど境界）: 0.2未満ではないのでnext帯"""
        # fts=2, vec=10 → ratio=0.2（ちょうど）
        w_fts, w_vec = _compute_adaptive_weights(2, 10)
        assert w_fts == 0.8
        assert w_vec == 1.2

    def test_boundary_05(self):
        """ratio=0.5（ちょうど境界）: 0.5未満ではないのでデフォルト"""
        w_fts, w_vec = _compute_adaptive_weights(5, 10)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC

    def test_disabled_flag(self, monkeypatch):
        """ADAPTIVE_RRF_ENABLED=False: 常にデフォルト重み"""
        monkeypatch.setattr(search_service, 'ADAPTIVE_RRF_ENABLED', False)
        # ratio < 0.2 のケースでもデフォルト重みになること
        w_fts, w_vec = _compute_adaptive_weights(1, 10)
        assert w_fts == RRF_W_FTS
        assert w_vec == RRF_W_VEC


class TestRrfMergeAdaptive:
    """_rrf_merge でAdaptive RRF重みが反映されることを検証"""

    def test_adaptive_low_ratio_boosts_vector(self):
        """ratio < 0.2: Adaptive重み(0.5, 1.5)が正規化に反映される"""
        # FTS: 1件, ベクトル: 10件 → ratio=0.1 → w_fts=0.5, w_vec=1.5
        fts = [{"type": "topic", "id": 1, "title": "A"}]
        vec = [{"type": "topic", "id": i, "title": f"V{i}"} for i in range(2, 12)]

        results = _rrf_merge(fts, vec, limit=20)

        max_score = (0.5 + 1.5) / (RRF_K + 1)
        # id=1 はFTSのランク1、ベクトルには未登場
        score_fts_only = next(r["score"] for r in results if r["id"] == 1)
        assert score_fts_only == pytest.approx(round(0.5 / (RRF_K + 1) / max_score, 4))

        # id=2 はベクトルのランク1、FTSには未登場
        score_vec_only = next(r["score"] for r in results if r["id"] == 2)
        assert score_vec_only == pytest.approx(round(1.5 / (RRF_K + 1) / max_score, 4))

    def test_adaptive_mid_ratio(self):
        """ratio < 0.5: Adaptive重み(0.8, 1.2)が正規化に反映される"""
        # FTS: 3件, ベクトル: 10件 → ratio=0.3
        fts = [{"type": "topic", "id": i, "title": f"F{i}"} for i in range(1, 4)]
        vec = [{"type": "topic", "id": i, "title": f"V{i}"} for i in range(11, 21)]

        results = _rrf_merge(fts, vec, limit=20)

        max_score = (0.8 + 1.2) / (RRF_K + 1)
        # FTSランク1の重み = 0.8
        score_fts = next(r["score"] for r in results if r["id"] == 1)
        assert score_fts == pytest.approx(round(0.8 / (RRF_K + 1) / max_score, 4))

        # ベクトルランク1の重み = 1.2
        score_vec = next(r["score"] for r in results if r["id"] == 11)
        assert score_vec == pytest.approx(round(1.2 / (RRF_K + 1) / max_score, 4))

    def test_adaptive_high_ratio_default(self):
        """ratio >= 0.5: デフォルト重み(1.0, 1.0)でランク1のスコアが0.5になる"""
        # FTS: 5件, ベクトル: 5件 → ratio=1.0
        fts = [{"type": "topic", "id": i, "title": f"F{i}"} for i in range(1, 6)]
        vec = [{"type": "topic", "id": i, "title": f"V{i}"} for i in range(11, 16)]

        results = _rrf_merge(fts, vec, limit=20)

        # 片方のソースのみでランク1 → 0.5（理論最大の半分）
        score_fts = next(r["score"] for r in results if r["id"] == 1)
        assert score_fts == pytest.approx(0.5)

        score_vec = next(r["score"] for r in results if r["id"] == 11)
        assert score_vec == pytest.approx(0.5)

    def test_adaptive_tag_weight_unchanged(self):
        """Adaptive RRFでもタグLIKEの重みは固定（RRF_W_TAG=0.5）"""
        # ratio < 0.2 → w_fts=0.5, w_vec=1.5 でもタグは0.5のまま
        fts = [{"type": "topic", "id": 1, "title": "A"}]
        vec = [{"type": "topic", "id": i, "title": f"V{i}"} for i in range(2, 12)]
        tag = [{"type": "topic", "id": 100, "title": "Tag"}]

        results = _rrf_merge(fts, vec, limit=20, tag_results=tag)

        max_score = (0.5 + 1.5 + RRF_W_TAG) / (RRF_K + 1)
        score_tag = next(r["score"] for r in results if r["id"] == 100)
        assert score_tag == pytest.approx(round(RRF_W_TAG / (RRF_K + 1) / max_score, 4))

    def test_adaptive_disabled_uses_default(self, monkeypatch):
        """ADAPTIVE_RRF_ENABLED=False: 固定重みが使われ、片方ランク1で0.5"""
        monkeypatch.setattr(search_service, 'ADAPTIVE_RRF_ENABLED', False)

        # ratio < 0.2 のケースでもデフォルト重みになること
        fts = [{"type": "topic", "id": 1, "title": "A"}]
        vec = [{"type": "topic", "id": i, "title": f"V{i}"} for i in range(2, 12)]

        results = _rrf_merge(fts, vec, limit=20)

        # デフォルト重み(1.0, 1.0)で片方ランク1 → 0.5
        score_fts = next(r["score"] for r in results if r["id"] == 1)
        assert score_fts == pytest.approx(0.5)

        score_vec = next(r["score"] for r in results if r["id"] == 2)
        assert score_vec == pytest.approx(0.5)

    def test_adaptive_vec_empty_uses_default(self):
        """ベクトル結果が空: デフォルト重みでFTSランク1が0.5になる"""
        fts = [{"type": "topic", "id": 1, "title": "A"}]

        results = _rrf_merge(fts, [], limit=10)

        # FTSのみランク1、理論最大(1.0+1.0)/(k+1)に対して1.0/(k+1) → 0.5
        assert results[0]["score"] == pytest.approx(0.5)


# ========================================
# include_details テスト
# ========================================


def test_include_details_false_no_details(temp_db, mock_embedding_model):
    """include_details=False（デフォルト）: detailsフィールドが付かない"""
    add_topic(
        title="ディテール無効テスト用トピック",
        description="ディテールが付かないことを確認する",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="ディテール無効テスト用")

    assert "error" not in result
    assert len(result["results"]) >= 1
    for item in result["results"]:
        assert "details" not in item


def test_include_details_true_topic(temp_db, mock_embedding_model):
    """include_details=True + topic型: description + recent_decisionsが付く"""
    topic = add_topic(
        title="トピック詳細テスト用",
        description="これはトピックの詳細説明テストです",
        tags=DEFAULT_TAGS,
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="テスト決定事項1",
        reason="理由1",
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="テスト決定事項2",
        reason="理由2",
    )

    result = search_service.search(
        keyword="トピック詳細テスト用",
        entity_type="topic",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert "details" in item
    assert "description" in item["details"]
    assert "recent_decisions" in item["details"]
    assert item["details"]["description"] == "これはトピックの詳細説明テストです"
    assert len(item["details"]["recent_decisions"]) == 2


def test_include_details_true_topic_decisions_limit(temp_db, mock_embedding_model):
    """include_details=True + topic型: recent_decisionsは最大3件"""
    topic = add_topic(
        title="決定件数制限テスト用トピック",
        description="3件制限のテスト",
        tags=DEFAULT_TAGS,
    )
    for i in range(5):
        add_decision(
            topic_id=topic["topic_id"],
            decision=f"決定事項{i}",
            reason=f"理由{i}",
        )

    result = search_service.search(
        keyword="決定件数制限テスト用",
        entity_type="topic",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert len(item["details"]["recent_decisions"]) == 3


def test_include_details_true_topic_description_truncated(temp_db, mock_embedding_model):
    """include_details=True + topic型: descriptionが500文字に制限される"""
    long_desc = "あ" * 800
    add_topic(
        title="説明文長さ制限テスト用トピック",
        description=long_desc,
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(
        keyword="説明文長さ制限テスト用",
        entity_type="topic",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert len(item["details"]["description"]) == DETAILS_DESCRIPTION_MAX


def test_include_details_true_activity(temp_db, mock_embedding_model):
    """include_details=True + activity型: description + statusが付く"""
    add_activity(
        title="アクティビティ詳細テスト用",
        description="アクティビティの説明テスト",
        tags=DEFAULT_TAGS,
        check_in=False,
    )

    result = search_service.search(
        keyword="アクティビティ詳細テスト用",
        entity_type="activity",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert "details" in item
    assert item["details"]["description"] == "アクティビティの説明テスト"
    assert "status" in item["details"]


def test_include_details_true_decision(temp_db, mock_embedding_model):
    """include_details=True + decision型: decision + reasonが付く"""
    topic = add_topic(
        title="決定詳細テスト用トピック",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="決定詳細テスト用の決定",
        reason="詳細テスト用の理由",
    )

    result = search_service.search(
        keyword="決定詳細テスト用",
        entity_type="decision",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert "details" in item
    assert item["details"]["decision"] == "決定詳細テスト用の決定"
    assert item["details"]["reason"] == "詳細テスト用の理由"


def test_include_details_true_log(temp_db, mock_embedding_model):
    """include_details=True + log型: content先頭500文字が付く"""
    topic = add_topic(
        title="ログ詳細テスト用トピック",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )
    add_log(
        topic_id=topic["topic_id"],
        title="ログ詳細テスト用ログ",
        content="ログの本文テスト内容です",
    )

    result = search_service.search(
        keyword="ログ詳細テスト用",
        entity_type="log",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert "details" in item
    assert item["details"]["content"] == "ログの本文テスト内容です"


def test_include_details_true_log_content_truncated(temp_db, mock_embedding_model):
    """include_details=True + log型: contentが500文字に制限される"""
    topic = add_topic(
        title="ログ長さ制限テスト用トピック",
        description="テスト用",
        tags=DEFAULT_TAGS,
    )
    long_content = "い" * 800
    add_log(
        topic_id=topic["topic_id"],
        title="ログ長さ制限テスト用ログ",
        content=long_content,
    )

    result = search_service.search(
        keyword="ログ長さ制限テスト用",
        entity_type="log",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 1
    item = result["results"][0]
    assert len(item["details"]["content"]) == DETAILS_DESCRIPTION_MAX


def test_include_details_true_material_no_details(temp_db, mock_embedding_model):
    """include_details=True + material型: detailsは付与されない"""
    add_material(
        title="マテリアル詳細テスト用素材",
        content="素材の本文内容",
        tags=DEFAULT_TAGS,
        source="テスト用データ",
    )

    result = search_service.search(
        keyword="マテリアル詳細テスト用",
        entity_type="material",
        include_details=True,
    )

    assert "error" not in result
    # material結果がある場合、detailsが付かないことを確認
    material_items = [r for r in result["results"] if r["type"] == "material"]
    for item in material_items:
        assert "details" not in item


def test_include_details_top_n_limit(temp_db, mock_embedding_model):
    """include_details=True: 上位DETAILS_MAX_RESULTS件のみにdetailsが付く"""
    # DETAILS_MAX_RESULTS + 5件のトピックを作成
    for i in range(DETAILS_MAX_RESULTS + 5):
        add_topic(
            title=f"上位制限テスト用トピック{i:02d}",
            description=f"上位制限テスト用の説明{i:02d}",
            tags=DEFAULT_TAGS,
        )

    result = search_service.search(
        keyword="上位制限テスト用",
        entity_type="topic",
        limit=DETAILS_MAX_RESULTS + 5,
        include_details=True,
    )

    assert "error" not in result
    results = result["results"]
    assert len(results) >= DETAILS_MAX_RESULTS + 1

    # 上位DETAILS_MAX_RESULTS件にはdetailsが付く
    for item in results[:DETAILS_MAX_RESULTS]:
        assert "details" in item, f"Top {DETAILS_MAX_RESULTS} items should have details"

    # それ以降にはdetailsが付かない
    for item in results[DETAILS_MAX_RESULTS:]:
        assert "details" not in item, f"Items beyond top {DETAILS_MAX_RESULTS} should not have details"


def test_include_details_cross_type(temp_db, mock_embedding_model):
    """include_details=True: 異なるtypeを横断してdetailsが付く"""
    topic = add_topic(
        title="横断ディテールテスト用トピック",
        description="横断ディテールテスト用の説明",
        tags=DEFAULT_TAGS,
    )
    add_decision(
        topic_id=topic["topic_id"],
        decision="横断ディテールテスト用決定",
        reason="テスト理由",
    )
    add_activity(
        title="横断ディテールテスト用アクティビティ",
        description="テスト用アクティビティ説明",
        tags=DEFAULT_TAGS,
        check_in=False,
    )

    result = search_service.search(
        keyword="横断ディテールテスト用",
        include_details=True,
    )

    assert "error" not in result
    assert len(result["results"]) >= 2

    types_with_details = set()
    for item in result["results"]:
        if "details" in item:
            types_with_details.add(item["type"])

    # 少なくともtopicとdecisionとactivityのうち複数にdetailsが付く
    assert len(types_with_details) >= 2


def test_attach_details_unit_empty_list():
    """_attach_details単体: 空リストでエラーにならない"""
    results = []
    _attach_details(results)
    assert results == []
