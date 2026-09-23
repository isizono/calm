"""vector_retrieve retriever 単体テスト。

embedding サーバー失敗時の None 返却と、共有 conn の使用を確認する。
ハイブリッド検索(FTS+ベクトル+RRF)としての振る舞いは test_hybrid_search 側の
統合テストで担保しているため、ここでは retriever のシグネチャ・null フォールバック・
例外ハンドリングに加え、filter-first KNN の候補集合選定というretriever固有の内部契約
(recall collapse対策)に焦点を当てる。
"""
import hashlib

import numpy as np
import pytest
from sqlite_vec import serialize_float32

import src.services.embedding_service as emb
from src.db import get_connection
from src.services import search_service
from src.services.search_service import _resolve_tag_ids_readonly, vector_retrieve
from src.services.topic_service import add_topic
from tests.helpers import make_search_context as _make_ctx

EMBEDDING_DIM = 384
DEFAULT_TAGS = ["domain:test"]



@pytest.fixture
def mock_embedding_model(monkeypatch):
    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            seed = int(hashlib.sha256((prefix_str + text).encode()).hexdigest(), 16) % (2**32)
            np.random.seed(seed)
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)
    yield


@pytest.fixture
def disable_embedding(monkeypatch):
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


def test_vector_retrieve_returns_none_when_embedding_disabled(temp_db, disable_embedding):
    """encode_query が None を返すと (= 埋め込みサーバー未稼働) vector_retrieve は None。"""
    add_topic(title="alpha topic", description="hello", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert result is None


def test_vector_retrieve_uses_shared_conn(temp_db, mock_embedding_model, monkeypatch):
    """vector_retrieve は共有 conn を使い、自前で get_connection() を呼ばない。"""
    add_topic(title="alpha topic", description="hello world", tags=DEFAULT_TAGS)

    call_count = {"n": 0}
    real_get_connection = search_service.get_connection

    def tracking_get_connection():
        call_count["n"] += 1
        return real_get_connection()

    monkeypatch.setattr(search_service, "get_connection", tracking_get_connection)

    conn = real_get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert call_count["n"] == 0


def test_vector_retrieve_returns_list_when_embedding_available(temp_db, mock_embedding_model):
    """埋め込みサーバー稼働時は AND モードで list を返す（None ではない）。"""
    add_topic(title="alpha topic", description="hello world", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert isinstance(result, list)


def test_vector_retrieve_or_mode_merges_per_keyword(temp_db, mock_embedding_model):
    """OR モードでは各キーワードを個別に埋め込み → 結果をマージして返す。"""
    add_topic(title="alpha topic", description="hello", tags=DEFAULT_TAGS)
    add_topic(title="beta topic", description="world", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(
            keywords=("alpha", "beta"),
            fts_keywords=("alpha", "beta"),
            keyword_mode="or",
        )
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    # OR モードで両 keyword に対応する KNN を回すので None ではないはず
    assert result is not None
    assert isinstance(result, list)


def test_vector_retrieve_or_mode_zero_hits_returns_empty_list_not_none(temp_db, mock_embedding_model):
    """OR モード + 複数キーワードで embedding 取得自体は全キーワードで成功したが
    ヒット0件（vec_index が空）の場合、None ではなく [] を返す。

    「使えたが該当なし」と「使えなかった」を区別する契約を OR + 複数キーワードの
    分岐でも満たすことを確認する（AND / 単一キーワード分岐は既存テストで担保済み）。
    """
    # add_topic を呼ばないため vec_index は空のまま。encode_query 自体は
    # mock_embedding_model によりどのキーワードでも成功する。
    conn = get_connection()
    try:
        ctx = _make_ctx(
            keywords=("alpha", "beta"),
            fts_keywords=("alpha", "beta"),
            keyword_mode="or",
        )
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert result == []


def test_vector_retrieve_or_mode_all_embeddings_fail_returns_none(temp_db, disable_embedding):
    """OR モード + 複数キーワードで全キーワードの embedding 取得自体に失敗した場合は None。

    zero-hits ケース（[] を返す）と区別できることを確認する。
    """
    conn = get_connection()
    try:
        ctx = _make_ctx(
            keywords=("alpha", "beta"),
            fts_keywords=("alpha", "beta"),
            keyword_mode="or",
        )
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert result is None


def _set_vec_embedding(conn, search_index_id: int, vector: list[float]) -> None:
    conn.execute("DELETE FROM vec_index WHERE rowid = ?", (search_index_id,))
    conn.execute(
        "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
        (search_index_id, serialize_float32(vector)),
    )


def test_vector_retrieve_filter_first_survives_recall_collapse(temp_db, mock_embedding_model, monkeypatch):
    """タグフィルタ付きベクトル検索は、対象がグローバルKNNのtop-fetch_limit圏外でも
    フィルタ集合内であれば取りこぼさない（recall collapse対策、フィルタ集合内の正確なtop-k）。

    グローバルKNN→post-filterの実装に戻すと、fetch_limit件を超えるデコイが全てクエリ
    ベクトルへ極めて近い位置に並んでグローバルtop-fetch_limitを独占するため、対象タグの
    エンティティは1件もそこに現れずフィルタ後0件になって落ちる。
    """
    fetch_limit = 5
    n_decoys = fetch_limit + 3

    for i in range(n_decoys):
        add_topic(title=f"recall collapse decoy {i}", description="filler", tags=["domain:decoy"])
    target = add_topic(title="recall collapse target", description="the real one", tags=["domain:target"])
    target_id = target["topic_id"]

    query_vec = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    decoy_vec = [1.0 - 1e-4] + [1e-5] * (EMBEDDING_DIM - 1)
    target_vec = [0.0] * (EMBEDDING_DIM - 1) + [1.0]

    conn = get_connection()
    try:
        decoy_rows = conn.execute(
            "SELECT id FROM search_index WHERE source_type = 'topic' AND title LIKE 'recall collapse decoy%'"
        ).fetchall()
        assert len(decoy_rows) == n_decoys
        for row in decoy_rows:
            _set_vec_embedding(conn, row["id"], decoy_vec)

        target_si_id = conn.execute(
            "SELECT id FROM search_index WHERE source_type = 'topic' AND source_id = ?",
            (target_id,),
        ).fetchone()["id"]
        _set_vec_embedding(conn, target_si_id, target_vec)
        conn.commit()

        monkeypatch.setattr(emb, "encode_query", lambda text: query_vec)

        # sanity: グローバルKNN(タグフィルタ無し)ではデコイのみがtop-fetch_limitを占め、
        # targetは現れない（recall collapse repro の前提が成立していることの確認）
        ctx_unfiltered = _make_ctx(keywords=("q",), fts_keywords=("q",), tag_ids=None, fetch_limit=fetch_limit)
        unfiltered_result = vector_retrieve(ctx_unfiltered, conn)
        assert unfiltered_result is not None
        assert len(unfiltered_result) == fetch_limit
        assert all(r["title"].startswith("recall collapse decoy") for r in unfiltered_result)

        # 本題: targetのタグでフィルタすると、グローバルには居ないtargetが返る
        target_tag_id = _resolve_tag_ids_readonly(conn, ["domain:target"])[0]
        ctx_filtered = _make_ctx(
            keywords=("q",), fts_keywords=("q",), tag_ids=(target_tag_id,), fetch_limit=fetch_limit,
        )
        filtered_result = vector_retrieve(ctx_filtered, conn)
    finally:
        conn.close()

    assert len(filtered_result) == 1
    assert filtered_result[0]["type"] == "topic"
    assert filtered_result[0]["id"] == target_id
    assert filtered_result[0]["title"] == "recall collapse target"
    # query_vec=[1,0,...,0] と target_vec=[0,...,0,1] のL2距離はsqrt(2)
    assert filtered_result[0]["distance"] == pytest.approx(2 ** 0.5, abs=1e-3)
