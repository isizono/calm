"""embeddingサービスのテスト（HTTPクライアント方式）"""
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


def test_start_server_uses_existing_infra_path(temp_db, monkeypatch):
    """_start_server: Popenに渡すserver_pathが実在する src/infra/embedding_server.py を指す"""
    import subprocess

    captured = {}
    sentinel = object()

    def capturing_popen(args, **kwargs):
        captured["args"] = args
        return sentinel  # _start_serverは子プロセスハンドルをそのまま返す

    # project rootをこのチェックアウト自身に固定する。
    # emb.__file__ = <root>/src/services/embedding_service.py なので parents[2] が <root>。
    root = Path(emb.__file__).resolve().parents[2]
    monkeypatch.setenv("CC_MEMORY_PROJECT_ROOT", str(root))
    monkeypatch.setattr(emb, "_project_root_cache", None)  # env反映のためキャッシュをクリア
    monkeypatch.setattr(subprocess, "Popen", capturing_popen)

    assert emb._start_server() is sentinel

    server_path = captured["args"][1]
    assert server_path == os.path.join(str(root), "src", "infra", "embedding_server.py")
    assert os.path.isfile(server_path)


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
        tags=["domain:cc-memory", "intent:design"],
    )

    # embedding生成テキストにタグ文字列が含まれている
    assert len(captured_texts) >= 1
    # 最後のencode_batch呼び出しがtopic用
    topic_text = captured_texts[-1]
    assert "domain:cc-memory" in topic_text
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
