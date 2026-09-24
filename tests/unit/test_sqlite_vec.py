"""sqlite-vec拡張のロードとvec_indexテーブルの動作テスト"""
import pytest
from sqlite_vec import serialize_float32
from src.db import get_connection


EMBEDDING_DIM = 384



# ========================================
# sqlite-vec拡張ロードのテスト
# ========================================


def test_sqlite_vec_loaded(temp_db):
    """sqlite-vec拡張がロードされている"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT vec_version()")
        version = cursor.fetchone()[0]
        assert version is not None
        assert isinstance(version, str)
    finally:
        conn.close()


def test_vec_index_table_exists(temp_db):
    """vec_indexテーブルがマイグレーションで作成されている"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_index'"
        )
        result = cursor.fetchone()
        assert result is not None
        assert result[0] == "vec_index"
    finally:
        conn.close()


# ========================================
# vec_index INSERT/SELECTのテスト
# ========================================


def test_vec_index_insert_and_select(temp_db):
    """vec_indexにembeddingをINSERT/SELECTできる"""
    conn = get_connection()
    try:
        embedding = [0.1] * EMBEDDING_DIM
        conn.execute(
            "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
            (1, serialize_float32(embedding)),
        )
        conn.commit()

        cursor = conn.execute(
            "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = 1",
            (serialize_float32(embedding),),
        )
        result = cursor.fetchone()
        assert result is not None
        assert result[0] == 1
        # 同一ベクトルなのでdistanceは0
        assert result[1] == pytest.approx(0.0)
    finally:
        conn.close()


def test_vec_index_knn_search(temp_db):
    """KNN検索: 最も近いベクトルが上位に返る"""
    conn = get_connection()
    try:
        # 方向の異なる3つのベクトルを挿入(vec_indexはcosine距離のためノルムでなく
        # 方向の違いで順位が決まる。定数倍しただけの平行なベクトルは区別できない)
        vec_a = [1.0, 0.0] + [0.0] * (EMBEDDING_DIM - 2)
        vec_b = [0.8, 0.6] + [0.0] * (EMBEDDING_DIM - 2)
        vec_c = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)

        conn.execute(
            "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
            (1, serialize_float32(vec_a)),
        )
        conn.execute(
            "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
            (2, serialize_float32(vec_b)),
        )
        conn.execute(
            "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
            (3, serialize_float32(vec_c)),
        )
        conn.commit()

        # vec_aに最も近い(方向が最も近い)2件を検索
        query = [0.9, 0.1] + [0.0] * (EMBEDDING_DIM - 2)
        cursor = conn.execute(
            "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = 2",
            (serialize_float32(query),),
        )
        results = cursor.fetchall()
        assert len(results) == 2
        # vec_a（rowid=1）が最も近いはず
        assert results[0][0] == 1
        # vec_b（rowid=2）が2番目
        assert results[1][0] == 2
        # distanceは単調増加
        assert results[0][1] <= results[1][1]
    finally:
        conn.close()


def test_vec_index_float384_dimension(temp_db):
    """384次元のベクトルを正しく格納できる"""
    conn = get_connection()
    try:
        # 各要素が異なる384次元ベクトル
        embedding = [float(i) / EMBEDDING_DIM for i in range(EMBEDDING_DIM)]
        conn.execute(
            "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
            (1, serialize_float32(embedding)),
        )
        conn.commit()

        cursor = conn.execute(
            "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = 1",
            (serialize_float32(embedding),),
        )
        result = cursor.fetchone()
        assert result is not None
        assert result[0] == 1
        assert result[1] == pytest.approx(0.0)
    finally:
        conn.close()


def test_vec_index_multiple_inserts(temp_db):
    """複数行のINSERTが正しく動作する"""
    conn = get_connection()
    try:
        for i in range(10):
            embedding = [float(i) / 10.0] * EMBEDDING_DIM
            conn.execute(
                "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                (i + 1, serialize_float32(embedding)),
            )
        conn.commit()

        cursor = conn.execute("SELECT count(*) FROM vec_index")
        count = cursor.fetchone()[0]
        assert count == 10
    finally:
        conn.close()
