"""embeddingサービスのテスト（HTTPクライアント方式）"""
import json
import os
import tempfile
import urllib.request
from pathlib import Path
import pytest
import numpy as np

from src.db import init_database, get_connection, execute_query
from src.services.topic_service import add_topic
from tests.helpers import add_decision
from src.services.activity_service import add_activity
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


@pytest.fixture(autouse=True)
def reset_spawn_cooldown(monkeypatch):
    """テスト間でspawn失敗クールダウンが持ち越されないようリセットする"""
    monkeypatch.setattr(emb, "_last_spawn_failed_at", None)


@pytest.fixture
def mock_embedding_server(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化"""

    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            # prefix + textのハッシュで決定論的に生成（サーバー側でのprefix付与を模擬）
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            np.random.seed(hash(prefix_str + text) % (2**32))
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)
    yield


# ========================================
# encode_document / encode_query のテスト
# ========================================


def test_encode_document_returns_embedding(temp_db, mock_embedding_server):
    """encode_document: 正常にembeddingが返る"""
    result = emb.encode_document("テスト文書")

    assert result is not None
    assert isinstance(result, list)
    assert len(result) == EMBEDDING_DIM
    assert all(isinstance(v, float) for v in result)


def test_encode_query_returns_embedding(temp_db, mock_embedding_server):
    """encode_query: 正常にembeddingが返る"""
    result = emb.encode_query("テストクエリ")

    assert result is not None
    assert isinstance(result, list)
    assert len(result) == EMBEDDING_DIM
    assert all(isinstance(v, float) for v in result)


def test_encode_document_uses_document_prefix(temp_db, monkeypatch):
    """encode_document: prefix "document" がサーバーに送られる"""
    captured_calls = []

    def capturing_encode_batch(texts, prefix):
        captured_calls.append((texts, prefix))
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist()]

    monkeypatch.setattr(emb, '_encode_batch', capturing_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    emb.encode_document("テスト文書")

    assert len(captured_calls) == 1
    assert captured_calls[0][0] == ["テスト文書"]
    assert captured_calls[0][1] == "document"


def test_encode_query_uses_query_prefix(temp_db, monkeypatch):
    """encode_query: prefix "query" がサーバーに送られる"""
    captured_calls = []

    def capturing_encode_batch(texts, prefix):
        captured_calls.append((texts, prefix))
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist()]

    monkeypatch.setattr(emb, '_encode_batch', capturing_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    emb.encode_query("テストクエリ")

    assert len(captured_calls) == 1
    assert captured_calls[0][0] == ["テストクエリ"]
    assert captured_calls[0][1] == "query"


# ========================================
# graceful degradation のテスト
# ========================================


def test_graceful_degradation_server_unavailable(temp_db, monkeypatch):
    """graceful degradation: サーバー接続失敗時にNoneを返す"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', False)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    result = emb.encode_document("テスト")

    assert result is None


# ========================================
# _ensure_initialized のテスト
# ========================================


def test_ensure_initialized_only_once(temp_db, monkeypatch):
    """_ensure_initialized: 2回目の呼び出しでサーバー起動を再試行しない"""
    call_count = 0

    def counting_ensure_server():
        nonlocal call_count
        call_count += 1
        return True

    def mock_encode_batch(texts, prefix):
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', counting_ensure_server)
    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)

    emb._ensure_initialized()
    emb._ensure_initialized()

    assert call_count == 1


def test_ensure_initialized_starts_backfill_thread_only_once_under_concurrency(temp_db, monkeypatch):
    """_ensure_initialized: 並行呼び出しでもバックフィルスレッドの起動は1回に直列化される

    _init_lock が無ければ、サーバー復帰直後の並行呼び出しが _server_initialized 更新前に
    互いをすり抜け、バックフィルを多重起動しうる。
    """
    import threading

    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', False)
    monkeypatch.setattr(emb, '_backfill_started', False)
    monkeypatch.setattr(emb, '_backfill_thread', None)

    entered = threading.Event()
    release = threading.Event()

    def blocking_ensure_server_running():
        entered.set()
        release.wait(timeout=5)
        return True

    backfill_calls = {"n": 0}

    def fake_backfill_embeddings():
        backfill_calls["n"] += 1
        return 0

    monkeypatch.setattr(emb, '_ensure_server_running', blocking_ensure_server_running)
    monkeypatch.setattr(emb, 'backfill_embeddings', fake_backfill_embeddings)
    monkeypatch.setattr(emb, 'backfill_topic_embeddings', lambda: 0)

    results = []
    t1 = threading.Thread(target=lambda: results.append(emb._ensure_initialized()))
    t1.start()
    assert entered.wait(timeout=5)  # t1が_init_lock内でブロック中

    t2 = threading.Thread(target=lambda: results.append(emb._ensure_initialized()))
    t2.start()

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    if emb._backfill_thread is not None:
        emb._backfill_thread.join(timeout=5)

    assert results == [True, True]
    assert backfill_calls["n"] == 1  # t2はロック取得後、_server_initialized済みで復帰しbackfillを開始しない


class TestReadPositiveIntEnv:
    """_read_positive_int_env: backfillチャンクサイズ等のenv上書き読み取り"""

    ENV_NAME = "CALM_EMBEDDING_BACKFILL_CHAR_BUDGET"

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(self.ENV_NAME, raising=False)
        assert emb._read_positive_int_env(self.ENV_NAME, 48_000) == 48_000

    def test_returns_env_value_when_valid(self, monkeypatch):
        monkeypatch.setenv(self.ENV_NAME, "100")
        assert emb._read_positive_int_env(self.ENV_NAME, 48_000) == 100

    def test_falls_back_to_default_on_invalid_string(self, monkeypatch):
        monkeypatch.setenv(self.ENV_NAME, "abc")
        assert emb._read_positive_int_env(self.ENV_NAME, 48_000) == 48_000

    def test_falls_back_to_default_on_zero_or_negative(self, monkeypatch):
        monkeypatch.setenv(self.ENV_NAME, "0")
        assert emb._read_positive_int_env(self.ENV_NAME, 48_000) == 48_000
        monkeypatch.setenv(self.ENV_NAME, "-5")
        assert emb._read_positive_int_env(self.ENV_NAME, 48_000) == 48_000


# ========================================
# insert_embedding のテスト
# ========================================


def test_insert_embedding_adds_to_vec_index(temp_db, mock_embedding_server):
    """insert_embedding: vec_indexにレコードが追加される"""
    topic = add_topic(
        title="テストトピック",
        description="テスト説明",
        tags=DEFAULT_TAGS,
    )

    # search_indexのIDを取得
    rows = execute_query(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        ("topic", topic["topic_id"]),
    )
    assert len(rows) > 0
    search_index_id = rows[0]["id"]

    # vec_indexにembeddingが存在するか確認
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
        count = cursor.fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# ========================================
# add系関数の統合テスト
# ========================================


def test_add_topic_creates_embedding(temp_db, mock_embedding_server):
    """add_topic後にvec_indexにembeddingが存在する"""
    topic = add_topic(
        title="Embedding統合テストトピック",
        description="vec_indexへの格納を検証する",
        tags=DEFAULT_TAGS,
    )

    assert "error" not in topic

    # search_indexのIDを取得
    rows = execute_query(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        ("topic", topic["topic_id"]),
    )
    assert len(rows) > 0
    search_index_id = rows[0]["id"]

    # vec_indexにembeddingが存在する
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
        count = cursor.fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_add_decision_creates_embedding(temp_db, mock_embedding_server):
    """add_decision後にvec_indexにembeddingが存在する"""
    topic = add_topic(
        title="テスト用トピック",
        description="テスト",
        tags=DEFAULT_TAGS,
    )
    dec = add_decision(
        topic_id=topic["topic_id"],
        decision="Embedding統合テスト決定",
        reason="vec_indexへの格納検証",
    )

    assert "error" not in dec

    # search_indexのIDを取得
    rows = execute_query(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        ("decision", dec["decision_id"]),
    )
    assert len(rows) > 0
    search_index_id = rows[0]["id"]

    # vec_indexにembeddingが存在する
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
        count = cursor.fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_add_activity_creates_embedding(temp_db, mock_embedding_server):
    """add_activity後にvec_indexにembeddingが存在する"""
    activity = add_activity(
        title="Embedding統合テストアクティビティ",
        description="vec_indexへの格納を検証する",
        tags=DEFAULT_TAGS,
        check_in=False,
    )

    assert "error" not in activity

    # search_indexのIDを取得
    rows = execute_query(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        ("activity", activity["activity_id"]),
    )
    assert len(rows) > 0
    search_index_id = rows[0]["id"]

    # vec_indexにembeddingが存在する
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
        count = cursor.fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# ========================================
# backfill のテスト
# ========================================


def test_backfill_fills_missing_embeddings(temp_db, monkeypatch):
    """backfill: search_indexにあってvec_indexにないレコードが埋められる"""

    def mock_encode_batch(texts, prefix):
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    # サーバーなしでtopicを作成（embeddingは生成されない）
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    topic = add_topic(
        title="バックフィルテストトピック",
        description="バックフィルの動作を検証する",
        tags=DEFAULT_TAGS,
    )

    # この時点ではvec_indexにembeddingがないことを確認
    rows = execute_query(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        ("topic", topic["topic_id"]),
    )
    assert len(rows) > 0
    search_index_id = rows[0]["id"]

    conn = get_connection()
    try:
        cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
        count = cursor.fetchone()[0]
        assert count == 0
    finally:
        conn.close()

    # サーバー稼働状態にしてバックフィル実行
    monkeypatch.setattr(emb, '_is_server_running', lambda: True)
    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)

    filled = emb.backfill_embeddings()

    # init_databaseで作成されたfirst_topicも含まれうるので、1以上であればOK
    assert filled >= 1

    # vec_indexにembeddingが追加されている
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
        count = cursor.fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_backfill_noop_when_all_filled(temp_db, mock_embedding_server, monkeypatch):
    """backfill: 全レコードが既にある場合は何もしない"""
    # _is_server_runningをTrueにしてbackfillが動くようにする
    monkeypatch.setattr(emb, '_is_server_running', lambda: True)

    # init_database由来の未バックフィルレコードを先に処理しておく
    emb.backfill_embeddings()

    # add_topicがembeddingも生成する（mock_embedding_serverがある）
    add_topic(
        title="全レコード存在テスト",
        description="バックフィル不要のケース",
        tags=DEFAULT_TAGS,
    )

    # 全レコードにembeddingがある状態でバックフィル実行
    filled = emb.backfill_embeddings()
    assert filled == 0


# ========================================
# backfill チャンク分割のテスト
# ========================================


def test_backfill_splits_into_multiple_chunks_and_commits_each(temp_db, monkeypatch):
    """backfill: BACKFILL_MAX_ITEMSを超える件数は複数チャンクのencode_batch呼出に分かれ、
    各チャンクごとにcommitされる（1リクエストに全件まとめて送らない）。
    """
    # サーバーなしでtopicを複数作成（embeddingは生成されない）
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    for i in range(3):
        add_topic(
            title=f"チャンク分割テストトピック{i}",
            description="バックフィルのチャンク分割を検証する",
            tags=DEFAULT_TAGS,
        )

    monkeypatch.setattr(emb, "BACKFILL_MAX_ITEMS", 1)
    monkeypatch.setattr(emb, "BACKFILL_CHAR_BUDGET", 10_000)
    monkeypatch.setattr(emb, "_is_server_running", lambda: True)

    calls = []

    def counting_encode_batch(texts, prefix):
        calls.append(list(texts))
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, "_encode_batch", counting_encode_batch)

    filled = emb.backfill_embeddings()

    # BACKFILL_MAX_ITEMS=1のため、全チャンクが1件ずつに分かれる
    assert len(calls) == filled
    assert all(len(c) == 1 for c in calls)
    assert filled >= 3  # 作成した3件のtopicが少なくとも含まれる


def test_backfill_partial_chunk_failure_keeps_already_committed_progress(temp_db, monkeypatch):
    """backfill: あるチャンクのencode失敗後、その種別の残りチャンクは諦めるが、
    それより前にcommit済みの成果は失われない。
    """
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)
    monkeypatch.setattr(emb, '_is_server_running', lambda: True)

    # init_database由来の未バックフィルレコード(first_topic等)を先に消化しておく。
    # 残したままだと後段のcall_countベースの失敗注入が意図しない対象に当たる。
    monkeypatch.setattr(
        emb, '_encode_batch',
        lambda texts, prefix: [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts],
    )
    emb.backfill_embeddings()

    topics = [
        add_topic(
            title=f"部分失敗テストトピック{i}",
            description="1チャンク目成功・2チャンク目以降失敗のケース",
            tags=DEFAULT_TAGS,
        )
        for i in range(3)
    ]

    monkeypatch.setattr(emb, "BACKFILL_MAX_ITEMS", 1)
    monkeypatch.setattr(emb, "BACKFILL_CHAR_BUDGET", 10_000)

    call_count = {"n": 0}

    def failing_after_first_encode_batch(texts, prefix):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]
        return None  # 2回目以降は失敗（サーバーダウン等を模す）

    monkeypatch.setattr(emb, "_encode_batch", failing_after_first_encode_batch)

    filled = emb.backfill_embeddings()

    assert filled == 1  # 1チャンク目(1件)だけ成功

    # 1チャンク目で処理された1件は既にvec_indexにcommit済みであること
    conn = get_connection()
    try:
        committed = 0
        for topic in topics:
            rows = execute_query(
                "SELECT id FROM search_index WHERE source_type = 'topic' AND source_id = ?",
                (topic["topic_id"],),
            )
            search_index_id = rows[0]["id"]
            count = conn.execute(
                "SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,)
            ).fetchone()[0]
            committed += count
        assert committed == 1
    finally:
        conn.close()


class TestChunkBackfillItems:
    """_chunk_backfill_items: 文字数予算・件数上限どちらか先に達した方でチャンクを区切る"""

    def test_splits_by_max_items(self, monkeypatch):
        monkeypatch.setattr(emb, "BACKFILL_MAX_ITEMS", 2)
        monkeypatch.setattr(emb, "BACKFILL_CHAR_BUDGET", 10_000)
        items = [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e")]

        chunks = emb._chunk_backfill_items(items)

        assert [len(c) for c in chunks] == [2, 2, 1]

    def test_splits_by_char_budget(self, monkeypatch):
        monkeypatch.setattr(emb, "BACKFILL_MAX_ITEMS", 100)
        monkeypatch.setattr(emb, "BACKFILL_CHAR_BUDGET", 5)
        items = [(1, "abc"), (2, "de"), (3, "fgh")]

        chunks = emb._chunk_backfill_items(items)

        assert chunks == [[(1, "abc"), (2, "de")], [(3, "fgh")]]

    def test_oversized_single_item_still_gets_its_own_chunk(self, monkeypatch):
        """1件のtextが単独でBACKFILL_CHAR_BUDGETを超えても進行が止まらない"""
        monkeypatch.setattr(emb, "BACKFILL_MAX_ITEMS", 100)
        monkeypatch.setattr(emb, "BACKFILL_CHAR_BUDGET", 3)
        items = [(1, "abcdefghij"), (2, "x")]

        chunks = emb._chunk_backfill_items(items)

        assert chunks == [[(1, "abcdefghij")], [(2, "x")]]

    def test_empty_items_returns_no_chunks(self):
        assert emb._chunk_backfill_items([]) == []


# ========================================
# _encode_batch: 切り詰め・ensure_ascii のテスト
# ========================================


class TestEncodeBatchRequestPayload:
    """_encode_batch: HTTPリクエスト直前の境界（urlopen）だけをmockし、
    実際に送信されるpayloadの中身を検証する。
    """

    def _capture_request(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({"embeddings": [[0.0] * EMBEDDING_DIM]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return captured

    def test_truncates_text_to_max_chars(self, monkeypatch):
        """TEXT_MAX_CHARSを超えるテキストは切り詰めて送信する"""
        monkeypatch.setattr(emb, "TEXT_MAX_CHARS", 10)
        captured = self._capture_request(monkeypatch)

        emb._encode_batch(["あ" * 20], "document")

        payload = json.loads(captured["body"])
        assert payload["texts"] == ["あ" * 10]

    def test_sends_japanese_text_without_unicode_escaping(self, monkeypatch):
        """ensure_ascii=Falseで送信し、\\uXXXX展開によるリクエスト肥大化を避ける"""
        monkeypatch.setattr(emb, "TEXT_MAX_CHARS", 1000)
        captured = self._capture_request(monkeypatch)

        emb._encode_batch(["日本語のテスト文書です"], "document")

        assert "日本語のテスト文書です".encode("utf-8") in captured["body"]
        assert b"\\u65e5" not in captured["body"]  # "日"のunicodeエスケープが含まれない


# ========================================
# embedding失敗時のgraceful degradation テスト
# ========================================


def test_add_topic_succeeds_when_embedding_fails(temp_db, monkeypatch):
    """embedding生成失敗時もadd_topic自体は成功する"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    topic = add_topic(
        title="Embedding失敗テスト",
        description="サーバー接続失敗時もtopic作成は成功する",
        tags=DEFAULT_TAGS,
    )

    assert "error" not in topic
    assert topic["topic_id"] is not None

    # vec_indexにはembeddingがない
    rows = execute_query(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        ("topic", topic["topic_id"]),
    )
    if rows:
        search_index_id = rows[0]["id"]
        conn = get_connection()
        try:
            cursor = conn.execute("SELECT count(*) FROM vec_index WHERE rowid = ?", (search_index_id,))
            count = cursor.fetchone()[0]
            assert count == 0
        finally:
            conn.close()


def test_add_decision_succeeds_when_embedding_fails(temp_db, monkeypatch):
    """embedding生成失敗時もadd_decision自体は成功する"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    topic = add_topic(
        title="テスト用トピック",
        description="テスト",
        tags=DEFAULT_TAGS,
    )

    dec = add_decision(
        topic_id=topic["topic_id"],
        decision="Embedding失敗テスト決定",
        reason="サーバー接続失敗時もdecision作成は成功する",
    )

    assert "error" not in dec
    assert dec["decision_id"] is not None


def test_add_activity_succeeds_when_embedding_fails(temp_db, monkeypatch):
    """embedding生成失敗時もadd_activity自体は成功する"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)

    activity = add_activity(
        title="Embedding失敗テストアクティビティ",
        description="サーバー接続失敗時もactivity作成は成功する",
        tags=DEFAULT_TAGS,
        check_in=False,
    )

    assert "error" not in activity
    assert activity["activity_id"] is not None


# ========================================
# サーバー障害からの回復テスト (#1)
# ========================================


def test_encode_batch_failure_resets_initialized_flag(temp_db, monkeypatch):
    """_encode_batch失敗時に_server_initializedがFalseにリセットされる"""
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    # urllib.request.urlopenを失敗させて本物の_encode_batchを通す
    def failing_urlopen(*args, **kwargs):
        raise ConnectionError("server crashed")

    monkeypatch.setattr(urllib.request, 'urlopen', failing_urlopen)

    result = emb.encode_document("テスト")

    assert result is None
    assert emb._server_initialized is False


def test_recovery_after_encode_batch_failure(temp_db, monkeypatch):
    """_encode_batch失敗後、次回呼び出しでサーバー再起動を試みる"""
    ensure_call_count = 0
    real_encode_batch = emb._encode_batch

    def counting_ensure_server():
        nonlocal ensure_call_count
        ensure_call_count += 1
        return True

    def mock_encode_batch(texts, prefix):
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', counting_ensure_server)
    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)

    # Phase 1: 初回起動 → _ensure_server_running が呼ばれる
    emb.encode_document("テスト1")
    assert ensure_call_count == 1
    assert emb._server_initialized is True

    # Phase 2: サーバー障害シミュレート（本物の_encode_batch + urlopen失敗）
    monkeypatch.setattr(emb, '_encode_batch', real_encode_batch)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("crash")))

    emb.encode_document("テスト2")
    assert emb._server_initialized is False  # フラグがリセットされた

    # Phase 3: 復旧 → _ensure_server_running が再度呼ばれる
    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    emb.encode_document("テスト3")
    assert ensure_call_count == 2


# ========================================
# _start_server 例外処理テスト (#2)
# ========================================


def test_start_server_failure_returns_none(temp_db, monkeypatch):
    """_start_server: subprocess.Popen失敗時にNoneを返す"""
    import subprocess

    def failing_popen(*args, **kwargs):
        raise FileNotFoundError("python not found")

    monkeypatch.setattr(subprocess, 'Popen', failing_popen)

    result = emb._start_server()
    assert result is None


def test_start_server_uses_module_execution_form(temp_db, monkeypatch):
    """_start_server: `-m src.infra.embedding_server` のモジュール実行形式でPopenを呼ぶ

    ファイルパスを直接実行する形式（`[sys.executable, server_path]`）だと
    sys.path[0]がembedding_server.py自身のディレクトリになり、内部の
    `from src.xxx import ...` がModuleNotFoundErrorでクラッシュする。
    """
    import subprocess

    captured = {}
    sentinel = object()

    def capturing_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel  # _start_serverは子プロセスハンドルをそのまま返す

    # project rootをこのチェックアウト自身に固定する。
    # emb.__file__ = <root>/src/services/embedding_service.py なので parents[2] が <root>。
    root = Path(emb.__file__).resolve().parents[2]
    monkeypatch.setenv("CALM_PROJECT_ROOT", str(root))
    monkeypatch.setattr(emb, "_project_root_cache", None)  # env反映のためキャッシュをクリア
    monkeypatch.setattr(subprocess, "Popen", capturing_popen)

    assert emb._start_server() is sentinel

    assert captured["args"][1:] == ["-m", "src.infra.embedding_server"]
    # cwdがproject root(モジュール解決の基点)に固定されていること
    assert captured["kwargs"]["cwd"] == str(root)
    # モジュール自体は実在すること（パス自体は渡さないが、参照先が存在しないと
    # -m実行が即失敗するため）
    assert os.path.isfile(os.path.join(str(root), "src", "infra", "embedding_server.py"))


def test_ensure_server_running_handles_start_failure(temp_db, monkeypatch):
    """_ensure_server_running: _start_server失敗時にFalseを返す"""
    monkeypatch.setattr(emb, '_is_server_running', lambda: False)
    monkeypatch.setattr(emb, '_start_server', lambda: None)

    result = emb._ensure_server_running()
    assert result is False


# ========================================
# spawn 直列化・タイムアウト回収テスト
# ========================================


class _FakeProc:
    """Popen の poll/terminate/wait/kill だけを模したスタブ。"""

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def test_ensure_server_running_single_spawn_under_concurrency(temp_db, monkeypatch):
    """_ensure_server_running: 並行呼び出しでも spawn は1回に直列化される"""
    import threading
    import time as time_mod

    state = {"running": False, "spawns": 0}
    spawn_entered = threading.Event()
    spawn_release = threading.Event()

    def fake_start():
        state["spawns"] += 1
        spawn_entered.set()
        spawn_release.wait(timeout=5)
        state["running"] = True
        return _FakeProc()

    monkeypatch.setattr(emb, "_is_server_running", lambda: state["running"])
    monkeypatch.setattr(emb, "_start_server", fake_start)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)

    results = []
    t1 = threading.Thread(target=lambda: results.append(emb._ensure_server_running()))
    t1.start()
    assert spawn_entered.wait(timeout=5)  # t1 がロック内で spawn 中

    # t2 は事前チェック（未起動）を通過後、ロック待ちに入る
    t2 = threading.Thread(target=lambda: results.append(emb._ensure_server_running()))
    t2.start()
    time_mod.sleep(0)  # 明示 yield（sleepはno-op化済みのため実質即時）
    spawn_release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results == [True, True]
    assert state["spawns"] == 1  # t2 はロック取得後の再チェックで spawn せず復帰


def test_ensure_server_running_kills_hung_child_on_timeout(temp_db, monkeypatch):
    """_ensure_server_running: bind前に固まった子はタイムアウト時に回収される"""
    import time as time_mod

    fake = _FakeProc(returncode=None)
    monkeypatch.setattr(emb, "_is_server_running", lambda: False)
    monkeypatch.setattr(emb, "_start_server", lambda: fake)
    monkeypatch.setattr(emb, "is_port_listening", lambda port: False)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)

    assert emb._ensure_server_running() is False
    assert fake.terminated is True


def test_ensure_server_running_spares_child_still_loading(temp_db, monkeypatch):
    """_ensure_server_running: bind済み（ロード進行中）の子はタイムアウトでも殺さない"""
    import time as time_mod

    fake = _FakeProc(returncode=None)
    monkeypatch.setattr(emb, "_is_server_running", lambda: False)
    monkeypatch.setattr(emb, "_start_server", lambda: fake)
    monkeypatch.setattr(emb, "is_port_listening", lambda port: True)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)

    assert emb._ensure_server_running() is False
    assert fake.terminated is False
    assert fake.killed is False


def test_ensure_server_running_gives_up_when_child_exits_early(temp_db, monkeypatch):
    """_ensure_server_running: 子が即終了し health も通らなければ30秒待たずFalse"""
    import time as time_mod

    sleep_count = {"n": 0}

    def counting_sleep(s):
        sleep_count["n"] += 1

    fake = _FakeProc(returncode=1)  # bind負け等で終了済み
    monkeypatch.setattr(emb, "_is_server_running", lambda: False)
    monkeypatch.setattr(emb, "_start_server", lambda: fake)
    monkeypatch.setattr(time_mod, "sleep", counting_sleep)

    assert emb._ensure_server_running() is False
    assert sleep_count["n"] < 60  # 60回ループを使い切らず早期リターン


def test_ensure_server_running_cooldown_blocks_respawn_after_failure(temp_db, monkeypatch):
    """_ensure_server_running: 起動失敗直後はクールダウンで再spawnしない"""
    import time as time_mod

    spawns = {"n": 0}

    def counting_start():
        spawns["n"] += 1
        return _FakeProc(returncode=1)  # 即死する子（モデルロード失敗等）

    monkeypatch.setattr(emb, "_is_server_running", lambda: False)
    monkeypatch.setattr(emb, "_start_server", counting_start)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)

    assert emb._ensure_server_running() is False
    assert spawns["n"] == 1
    # クールダウン中の再呼び出しはspawnせずFalse
    assert emb._ensure_server_running() is False
    assert spawns["n"] == 1
    # クールダウン経過後は再spawnを試みる
    monkeypatch.setattr(
        emb, "_last_spawn_failed_at",
        time_mod.time() - emb._SPAWN_RETRY_COOLDOWN_SEC - 1,
    )
    assert emb._ensure_server_running() is False
    assert spawns["n"] == 2


def test_ensure_server_running_success_clears_cooldown(temp_db, monkeypatch):
    """_ensure_server_running: 起動成功でクールダウンがクリアされる"""
    import time as time_mod

    monkeypatch.setattr(emb, "_is_server_running", lambda: False)
    monkeypatch.setattr(emb, "_start_server", lambda: _FakeProc(returncode=1))
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    assert emb._ensure_server_running() is False
    assert emb._last_spawn_failed_at is not None

    # 次のspawnが成功するケース（1回目のhealth checkで即ready）
    monkeypatch.setattr(emb, "_last_spawn_failed_at", None)
    calls = {"n": 0}

    def health_after_start():
        calls["n"] += 1
        return calls["n"] > 2  # 事前チェック・ロック内再チェックはFalse、以降True

    monkeypatch.setattr(emb, "_is_server_running", health_after_start)
    monkeypatch.setattr(emb, "_start_server", lambda: _FakeProc(returncode=None))
    assert emb._ensure_server_running() is True
    assert emb._last_spawn_failed_at is None


# ========================================
# embedding生成にタグ含有テスト
# ========================================


def test_embedding_text_includes_tags(temp_db, monkeypatch):
    """embedding生成テキストにタグ文字列が含まれる"""
    captured_texts = []

    def capturing_encode_batch(texts, prefix):
        captured_texts.extend(texts)
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_encode_batch', capturing_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    add_topic(
        title="タグ含有テストトピック",
        description="テスト説明",
        tags=["domain:calm", "intent:design"],
    )

    # embedding生成テキストにタグ文字列が含まれている
    assert len(captured_texts) >= 1
    # 最後のencode_batch呼び出しがtopic用
    topic_text = captured_texts[-1]
    assert "domain:calm" in topic_text
    assert "intent:design" in topic_text


def test_regenerate_embedding(temp_db, monkeypatch):
    """regenerate_embedding: エンティティのembeddingがタグ付きで再生成される"""
    captured_texts = []

    def capturing_encode_batch(texts, prefix):
        captured_texts.extend(texts)
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_encode_batch', capturing_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    topic = add_topic(
        title="再生成テストトピック",
        description="再生成テスト説明",
        tags=["domain:test"],
    )

    captured_texts.clear()

    # regenerate_embeddingを呼び出す
    emb.regenerate_embedding("topic", topic["topic_id"])

    # 再生成されたテキストにもタグが含まれる
    assert len(captured_texts) >= 1
    regen_text = captured_texts[-1]
    assert "再生成テストトピック" in regen_text
    assert "domain:test" in regen_text


def test_regenerate_embedding_nonexistent_entity(temp_db, monkeypatch):
    """regenerate_embedding: 存在しないエンティティでもエラーにならない"""
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    def mock_encode_batch(texts, prefix):
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)

    # 存在しないエンティティでもエラーにならない（graceful degradation）
    emb.regenerate_embedding("topic", 999999)
    emb.regenerate_embedding("invalid_type", 1)


def test_update_tag_canonical_regenerates_embedding(temp_db, monkeypatch):
    """update_tag canonical設定時に影響エンティティのembeddingが再生成される（E2E）"""
    captured_texts = []

    def capturing_encode_batch(texts, prefix):
        captured_texts.extend(texts)
        return [np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist() for _ in texts]

    monkeypatch.setattr(emb, '_encode_batch', capturing_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)

    # canonical先のタグを持つトピックと、エイリアス元のタグを持つトピックを作成
    # canonical先（new-tag）を先に作っておく必要がある
    add_topic(
        title="canonical先トピック",
        description="new-tagを持つ",
        tags=["domain:test", "new-tag"],
    )
    topic = add_topic(
        title="canonical再生成テスト",
        description="テスト説明",
        tags=["domain:test", "old-tag"],
    )

    captured_texts.clear()

    # old-tagをnew-tagのcanonicalに設定（old-tag → new-tagに付け替え）
    from src.services.tag_service import update_tag
    result = update_tag("old-tag", canonical="new-tag")
    assert "error" not in result, f"update_tag failed: {result}"

    # 影響エンティティのembeddingが再生成されたことを確認
    assert len(captured_texts) >= 1
    # 再生成テキストに元のトピックのタイトルが含まれる
    all_regen_text = " ".join(captured_texts)
    assert "canonical再生成テスト" in all_regen_text
