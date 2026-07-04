"""topic_vec 索引の書込・バックフィル・削除整合のテスト。

topic routing 専用のベクトル索引 topic_vec に対する add_topic 経由の書込、
バックフィル（vec_index からの再利用・二重エンコード無し）、削除経路を検証する。
"""
import os
import tempfile

import numpy as np
import pytest
from sqlite_vec import serialize_float32

from src.db import get_connection, init_database
from src.services.topic_service import add_topic
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
def mock_embedding_server(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化。呼び出し回数も記録する。"""
    call_count = {"n": 0}

    def mock_encode_batch(texts, prefix):
        call_count["n"] += 1
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            np.random.seed(hash(prefix_str + text) % (2**32))
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    yield call_count


def _topic_vec_row(conn, topic_id):
    return conn.execute(
        "SELECT rowid, embedding FROM topic_vec WHERE rowid = ?", (topic_id,)
    ).fetchone()


# ========================================
# add_topic 経由の書込
# ========================================


def test_add_topic_writes_topic_vec_with_rowid_eq_topic_id(temp_db, mock_embedding_server):
    """add_topic後、topic_vecにrowid=topic_idでレコードが1件だけ存在する"""
    topic = add_topic(
        title="topic_vec書込テスト",
        description="rowid対応を検証する",
        tags=DEFAULT_TAGS,
    )
    assert "error" not in topic
    topic_id = topic["topic_id"]

    conn = get_connection()
    try:
        row = _topic_vec_row(conn, topic_id)
        assert row is not None
        assert row["rowid"] == topic_id
    finally:
        conn.close()


def test_add_topic_does_not_double_encode(temp_db, mock_embedding_server):
    """add_topicはvec_index用に生成したembeddingをtopic_vecでも再利用し、
    topic_vec用に追加でencode_batchを呼ばない（二重エンコード無し）"""
    call_count = mock_embedding_server
    add_topic(
        title="二重エンコード検証トピック",
        description="topic_vec書込のためにencode_batchが増えないことを確認する",
        tags=DEFAULT_TAGS,
    )
    # add_topic 1回あたりのencode_batch呼び出しはtopic本文分の1回のみ
    # （類似トピック検索は生成済みembeddingを再利用するため追加呼び出しは発生しない）
    assert call_count["n"] == 1


def test_add_topic_vec_embedding_matches_vec_index(temp_db, mock_embedding_server):
    """topic_vecに書かれたembeddingはvec_indexに書かれたものと同一（bit-identical）"""
    topic = add_topic(
        title="embedding一致テストトピック",
        description="topic_vecとvec_indexが同じベクトルを持つことを確認する",
        tags=DEFAULT_TAGS,
    )
    topic_id = topic["topic_id"]

    conn = get_connection()
    try:
        search_index_id = conn.execute(
            "SELECT id FROM search_index WHERE source_type = 'topic' AND source_id = ?",
            (topic_id,),
        ).fetchone()["id"]
        vec_index_row = conn.execute(
            "SELECT embedding FROM vec_index WHERE rowid = ?", (search_index_id,)
        ).fetchone()
        topic_vec_row = _topic_vec_row(conn, topic_id)
        assert vec_index_row is not None
        assert topic_vec_row is not None
        assert vec_index_row["embedding"] == topic_vec_row["embedding"]
    finally:
        conn.close()


def test_add_topic_succeeds_when_embedding_fails(temp_db, monkeypatch):
    """embedding生成失敗時もadd_topic自体は成功し、topic_vecにも何も書かれない"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    topic = add_topic(
        title="Embedding失敗テスト",
        description="サーバー接続失敗時もtopic作成は成功する",
        tags=DEFAULT_TAGS,
    )
    assert "error" not in topic
    topic_id = topic["topic_id"]

    conn = get_connection()
    try:
        assert _topic_vec_row(conn, topic_id) is None
    finally:
        conn.close()


# ========================================
# KNN の母集団が topic のみであること
# ========================================


def test_knn_population_is_topic_only(temp_db, mock_embedding_server):
    """topic_vecのKNN母集団はtopicのみで、decision等の他entityが混入しない"""
    topic_ids = []
    for i in range(3):
        topic = add_topic(
            title=f"KNN母集団テスト{i}",
            description="topicのみが母集団であることを確認する",
            tags=DEFAULT_TAGS,
        )
        topic_ids.append(topic["topic_id"])

    from tests.helpers import add_decision

    for i in range(5):
        dec = add_decision(
            topic_id=topic_ids[0],
            decision=f"母集団攪乱decision{i}",
            reason="topic_vecに混入しないことを確認する",
        )
        assert "error" not in dec

    conn = get_connection()
    try:
        query_blob = serialize_float32([0.0] * EMBEDDING_DIM)
        rows = conn.execute(
            "SELECT rowid FROM topic_vec WHERE embedding MATCH ? AND k = ?",
            (query_blob, 50),
        ).fetchall()
        result_ids = {r["rowid"] for r in rows}
        # 母集団に decision の rowid（search_index.id 由来）が混入していない
        assert result_ids.issubset(set(topic_ids))
    finally:
        conn.close()


# ========================================
# insert / delete ヘルパー関数単体
# ========================================


def test_insert_topic_embedding_with_conn(temp_db):
    """insert_topic_embedding_with_conn: 渡したconnでtopic_vecに1行追加される"""
    topic = add_topic(title="insert単体テスト", description="テスト", tags=DEFAULT_TAGS)
    topic_id = topic["topic_id"]

    embedding = [0.5] * EMBEDDING_DIM
    conn = get_connection()
    try:
        emb.insert_topic_embedding_with_conn(conn, topic_id, embedding)
        conn.commit()
        row = _topic_vec_row(conn, topic_id)
        assert row is not None
    finally:
        conn.close()


def test_insert_topic_embedding_with_conn_upserts(temp_db):
    """insert_topic_embedding_with_conn: 既存rowidへの再INSERTはUPSERT（DELETE+INSERT）される"""
    topic = add_topic(title="upsertテスト", description="テスト", tags=DEFAULT_TAGS)
    topic_id = topic["topic_id"]

    conn = get_connection()
    try:
        emb.insert_topic_embedding_with_conn(conn, topic_id, [0.1] * EMBEDDING_DIM)
        emb.insert_topic_embedding_with_conn(conn, topic_id, [0.9] * EMBEDDING_DIM)
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM topic_vec WHERE rowid = ?", (topic_id,)
        ).fetchall()
        assert len(rows) == 1, "同一rowidへの再INSERTで行が重複してはならない"
    finally:
        conn.close()


def test_delete_topic_embedding_with_conn(temp_db, mock_embedding_server):
    """delete_topic_embedding_with_conn: topic_vecから対象行のみ削除される"""
    topic = add_topic(title="削除テスト", description="テスト", tags=DEFAULT_TAGS)
    topic_id = topic["topic_id"]

    conn = get_connection()
    try:
        assert _topic_vec_row(conn, topic_id) is not None
        emb.delete_topic_embedding_with_conn(conn, topic_id)
        conn.commit()
        assert _topic_vec_row(conn, topic_id) is None
    finally:
        conn.close()


# ========================================
# backfill_topic_embeddings: vec_index からの再利用（二重エンコード無し）
# ========================================


def test_backfill_reuses_vec_index_embedding_without_reencoding(temp_db, monkeypatch):
    """backfill_topic_embeddings: vec_indexに既存embeddingがあるtopicは
    encode_batchを呼ばずにtopic_vecへ複製される"""
    call_count = {"n": 0}

    def counting_encode_batch(texts, prefix):
        call_count["n"] += 1
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_encode_batch', counting_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_is_server_running', lambda: True)

    topic = add_topic(title="バックフィル再利用テスト", description="テスト", tags=DEFAULT_TAGS)
    topic_id = topic["topic_id"]
    assert call_count["n"] == 1  # add_topic時の1回のみ

    # topic_vecから一旦消し、backfillで復元させる
    conn = get_connection()
    try:
        conn.execute("DELETE FROM topic_vec WHERE rowid = ?", (topic_id,))
        conn.commit()
        assert _topic_vec_row(conn, topic_id) is None
    finally:
        conn.close()

    filled = emb.backfill_topic_embeddings()
    assert filled >= 1
    # backfillはvec_indexの既存embeddingを複製するだけで、追加のencode_batch呼び出しは発生しない
    assert call_count["n"] == 1

    conn = get_connection()
    try:
        assert _topic_vec_row(conn, topic_id) is not None
    finally:
        conn.close()


def test_backfill_skips_topic_without_vec_index_embedding(temp_db, monkeypatch):
    """backfill_topic_embeddings: vec_indexにもembeddingが無いtopicは対象外のまま"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    topic = add_topic(
        title="vec_indexも無いtopic", description="サーバー未起動で作成", tags=DEFAULT_TAGS
    )
    topic_id = topic["topic_id"]

    # backfill 自体はサーバー復帰後に走る想定。vec_index が無いこのtopicが
    # 複製対象外であることを検証するため、backfillガードは通過させる。
    monkeypatch.setattr(emb, '_is_server_running', lambda: True)
    filled = emb.backfill_topic_embeddings()

    conn = get_connection()
    try:
        assert _topic_vec_row(conn, topic_id) is None
    finally:
        conn.close()
    # このtopic分は複製されないが、他のtopic（init_database由来のfirst_topic等）が
    # 既にvec_indexを持っていれば0件とは限らないため filled の値自体は断定しない


def test_backfill_noop_when_all_filled(temp_db, mock_embedding_server, monkeypatch):
    """backfill_topic_embeddings: 全topicが既にtopic_vecを持つ場合は0を返す"""
    monkeypatch.setattr(emb, '_is_server_running', lambda: True)
    add_topic(title="全件充足テスト", description="テスト", tags=DEFAULT_TAGS)
    # 既存の未充足分（init_database由来等）を先に埋めておく
    emb.backfill_topic_embeddings()

    filled = emb.backfill_topic_embeddings()
    assert filled == 0


def test_ensure_initialized_runs_topic_backfill(temp_db, monkeypatch):
    """_ensure_initialized: backfill_embeddings と同じ経路でbackfill_topic_embeddingsも呼ばれる"""
    calls = []

    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', False)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: True)
    monkeypatch.setattr(emb, 'backfill_embeddings', lambda: calls.append('embeddings') or 0)
    monkeypatch.setattr(emb, 'backfill_topic_embeddings', lambda: calls.append('topic_embeddings') or 0)

    emb._ensure_initialized()

    assert calls == ['embeddings', 'topic_embeddings']

    # 2回目は _backfill_done により再実行されない
    emb._ensure_initialized()
    assert calls == ['embeddings', 'topic_embeddings']
