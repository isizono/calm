"""search_telemetry / fetch_telemetry テーブル書込のテスト

検証項目:
1. migration 0041 後に search_telemetry テーブルが存在する (id / query / parameters /
   result_count / timestamp カラムを持つ)
2. search() を呼ぶと search_telemetry に行が追加され、query / parameters / result_count
   が期待する JSON / 整数で記録される
3. 書込は別スレッドで行われる (`_record_search_telemetry_async` が Thread を返す)
4. 書込中に例外が発生しても search() の戻り値は通常通りで、本体を壊さない
5. search_telemetry に results_json / diagnostics_json 列が追加され、search() 呼出ごとに
   返却ページと retriever 内訳が記録される
6. fetch_telemetry テーブルが新設され、get_by_ids 呼出が非同期で記録される
7. fetch_telemetry の書込失敗は get_by_ids の戻り値に波及しない
8. search_telemetry / fetch_telemetry に caller_session_id 列が追加され、search() /
   get_by_ids() に渡した相関キーが記録される（未指定時は NULL）
9. _build_diagnostics の分岐（vec_hits の整数/None/0、tag_hits>0、qe_expansions 非空、
   methods_used への vector / tag_like 混入）が期待通り diagnostics に反映される
"""
import json
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services import search_service
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """telemetry テストではベクトル検索を無効化して FTS のみで決定論的に動かす"""
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def capture_telemetry_threads(monkeypatch):
    """search() が起動する telemetry 書込 thread を捕捉して join() できるようにする"""
    threads = []
    original = search_service._record_search_telemetry_async

    def wrapped(*args, **kwargs):
        thread = original(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(search_service, "_record_search_telemetry_async", wrapped)
    return threads


@pytest.fixture
def capture_fetch_telemetry_threads(monkeypatch):
    """get_by_ids が起動する fetch_telemetry 書込 thread を捕捉して join() できるようにする"""
    threads = []
    original = search_service._record_fetch_telemetry_async

    def wrapped(*args, **kwargs):
        thread = original(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(search_service, "_record_fetch_telemetry_async", wrapped)
    return threads


def _wait_for_telemetry(threads, timeout=5.0):
    for t in threads:
        if t is not None:
            t.join(timeout=timeout)


def _fetch_all_telemetry():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, query, parameters, result_count, results_json, diagnostics_json, "
            "caller_session_id, timestamp "
            "FROM search_telemetry ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _fetch_all_fetch_telemetry():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, tool, items_json, caller_session_id, timestamp "
            "FROM fetch_telemetry ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_search_telemetry_table_schema(temp_db):
    """migration 0041 + 0054 で search_telemetry テーブルと必要なカラムが作られる"""
    conn = get_connection()
    try:
        rows = conn.execute("PRAGMA table_info(search_telemetry)").fetchall()
    finally:
        conn.close()
    columns = {r["name"]: r for r in rows}
    assert {
        "id", "query", "parameters", "result_count",
        "results_json", "diagnostics_json", "caller_session_id", "timestamp",
    }.issubset(columns)
    assert columns["query"]["notnull"] == 1
    assert columns["parameters"]["notnull"] == 1
    assert columns["result_count"]["notnull"] == 1
    assert columns["timestamp"]["notnull"] == 1
    # results_json / diagnostics_json / caller_session_id は既存行 (NULL) との後方互換のため NULL 許容
    assert columns["results_json"]["notnull"] == 0
    assert columns["diagnostics_json"]["notnull"] == 0
    assert columns["caller_session_id"]["notnull"] == 0


def test_search_telemetry_index_on_timestamp(temp_db):
    """timestamp カラムに index が張られている (時系列集計の基盤)"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='search_telemetry'"
        ).fetchall()
    finally:
        conn.close()
    index_names = {r["name"] for r in rows}
    assert "idx_search_telemetry_timestamp" in index_names


def test_fetch_telemetry_table_schema(temp_db):
    """migration 0054 で fetch_telemetry テーブルと必要なカラムが作られる"""
    conn = get_connection()
    try:
        rows = conn.execute("PRAGMA table_info(fetch_telemetry)").fetchall()
    finally:
        conn.close()
    columns = {r["name"]: r for r in rows}
    assert {"id", "tool", "items_json", "caller_session_id", "timestamp"}.issubset(columns)
    assert columns["tool"]["notnull"] == 1
    assert columns["items_json"]["notnull"] == 1
    assert columns["timestamp"]["notnull"] == 1
    # caller_session_id は MCP context 外の呼出で NULL になるため NULL 許容
    assert columns["caller_session_id"]["notnull"] == 0


def test_fetch_telemetry_index_on_timestamp(temp_db):
    """fetch_telemetry.timestamp に index が張られている"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='fetch_telemetry'"
        ).fetchall()
    finally:
        conn.close()
    index_names = {r["name"] for r in rows}
    assert "idx_fetch_telemetry_timestamp" in index_names


def test_search_records_telemetry_row(temp_db, capture_telemetry_threads):
    """search() 呼出ごとに search_telemetry に 1 行追加される"""
    add_topic(
        title="テレメトリ書込テスト用トピック",
        description="search 呼出でテレメトリが書かれることを検証する",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="テレメトリ書込テスト")
    assert "error" not in result

    _wait_for_telemetry(capture_telemetry_threads)

    rows = _fetch_all_telemetry()
    assert len(rows) == 1
    row = rows[0]
    assert json.loads(row["query"]) == "テレメトリ書込テスト"
    assert row["result_count"] == result["total_count"]
    params = json.loads(row["parameters"])
    assert params["keyword_mode"] == "and"
    assert params["limit"] == 10
    assert params["offset"] == 0
    assert "entity_type" in params
    assert "domain" in params
    assert "date_after" in params
    assert "date_before" in params
    assert "include_details" in params
    assert "tags" in params
    assert row["timestamp"] is not None
    # caller_session_id 未指定の直接呼出は NULL で記録される
    assert row["caller_session_id"] is None

    # results_json: 返却ページの (type, id, final_score)
    results = json.loads(row["results_json"])
    assert len(results) == result["total_count"]
    returned_ids = {(r["type"], r["id"]) for r in results}
    assert ("topic", result["results"][0]["id_raw"]) in returned_ids
    for r in results:
        assert set(r.keys()) == {"type", "id", "final_score"}

    # diagnostics_json: retriever 内訳
    diagnostics = json.loads(row["diagnostics_json"])
    assert diagnostics["fts_hits"] >= 1
    assert diagnostics["vec_hits"] is None  # disable_embedding によりベクトル検索無効
    assert diagnostics["tag_hits"] == 0
    assert diagnostics["methods_used"] == result["search_methods_used"]
    assert diagnostics["candidate_set_size"] is None
    assert diagnostics["qe_expansions"] == []
    assert set(diagnostics["adaptive_weights"].keys()) == {"w_fts", "w_vec"}


def test_search_records_list_keyword_and_parameters(temp_db, capture_telemetry_threads):
    """配列 keyword と非デフォルトパラメータが parameters JSON に保存される"""
    add_topic(
        title="複合キーワード テレメトリ 検証",
        description="リストキーワードのスナップショット記録を検証する",
        tags=["domain:test", "telemetry"],
    )

    search_service.search(
        keyword=["複合キーワード", "テレメトリ"],
        tags=["domain:test"],
        entity_type="topic",
        limit=5,
        offset=0,
        keyword_mode="and",
        domain=None,
    )

    _wait_for_telemetry(capture_telemetry_threads)

    rows = _fetch_all_telemetry()
    assert len(rows) == 1
    assert json.loads(rows[0]["query"]) == ["複合キーワード", "テレメトリ"]
    params = json.loads(rows[0]["parameters"])
    assert params["tags"] == ["domain:test"]
    assert params["entity_type"] == "topic"
    assert params["limit"] == 5
    assert params["keyword_mode"] == "and"


def test_telemetry_write_runs_in_separate_thread(temp_db):
    """`_record_search_telemetry_async` が起動した Thread を返し、別スレッドで走る"""
    import threading as _threading

    main_thread_id = _threading.get_ident()
    captured_thread_id = []

    original_get_conn = search_service._telemetry_get_connection

    def tracking_get_conn():
        captured_thread_id.append(_threading.get_ident())
        return original_get_conn()

    search_service._telemetry_get_connection = tracking_get_conn
    try:
        thread = search_service._record_search_telemetry_async(
            query="thread-check",
            parameters={"limit": 10},
            result_count=0,
        )
        assert isinstance(thread, _threading.Thread)
        assert thread.daemon is True
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    finally:
        search_service._telemetry_get_connection = original_get_conn

    assert captured_thread_id, "telemetry 書込で _telemetry_get_connection が呼ばれていない"
    assert all(tid != main_thread_id for tid in captured_thread_id), (
        "telemetry 書込が呼出元と同じスレッドで実行されている"
    )


def test_search_unaffected_when_thread_start_fails(
    temp_db, monkeypatch, caplog
):
    """Thread.start() が失敗しても search() の戻り値は壊れず、警告ログが出る

    threading.Thread.start が ``RuntimeError: can't start new thread`` を出す
    シナリオを模す。search() 外側の try で DATABASE_ERROR 化されないことを保証。
    """
    add_topic(
        title="thread start 失敗フォールバック検証用トピック",
        description="thread 起動失敗時に search 本体が影響を受けないことを検証する",
        tags=DEFAULT_TAGS,
    )

    import threading as _threading

    original_thread_cls = search_service.threading.Thread

    class FailingThread(original_thread_cls):
        def start(self):
            raise RuntimeError("simulated thread start failure")

    monkeypatch.setattr(search_service.threading, "Thread", FailingThread)

    with caplog.at_level("WARNING"):
        result = search_service.search(keyword="thread start 失敗フォールバック検証")

    assert "error" not in result
    assert "results" in result
    assert any(
        "search_telemetry thread start failed" in record.message
        for record in caplog.records
    ), f"warning log が出ていない: {[r.message for r in caplog.records]}"


def test_search_unaffected_when_telemetry_write_fails(
    temp_db, capture_telemetry_threads, monkeypatch, caplog
):
    """書込中に例外が出ても search() の戻り値は壊れず、警告ログが出る"""
    add_topic(
        title="書込失敗フォールバック検証用トピック",
        description="例外発生時に search 本体が影響を受けないことを検証する",
        tags=DEFAULT_TAGS,
    )

    real_get_connection = search_service._telemetry_get_connection
    call_counter = {"writer": 0}

    def flaky_get_connection():
        # telemetry 書込スレッドからの呼び出し時のみ例外を出して
        # フォールバック挙動を検証する。search 本体は通常通り動く。
        call_counter["writer"] += 1
        raise RuntimeError("simulated telemetry write failure")

    monkeypatch.setattr(search_service, "_telemetry_get_connection", flaky_get_connection)

    with caplog.at_level("WARNING"):
        result = search_service.search(keyword="書込失敗フォールバック検証")

    _wait_for_telemetry(capture_telemetry_threads)

    assert "error" not in result
    assert "results" in result
    assert call_counter["writer"] >= 1
    assert any(
        "search_telemetry write failed" in record.message for record in caplog.records
    ), f"warning log が出ていない: {[r.message for r in caplog.records]}"


def test_get_by_ids_records_fetch_telemetry(temp_db, capture_fetch_telemetry_threads):
    """get_by_ids 呼出ごとに fetch_telemetry に 1 行追加される"""
    topic = add_topic(
        title="fetch telemetry 記録テスト",
        description="get_by_ids 呼出で fetch_telemetry が書かれることを検証する",
        tags=DEFAULT_TAGS,
    )
    decision = add_decisions([{
        "topic_id": topic["topic_id"],
        "decision": "fetch telemetry 検証用の決定内容",
        "reason": "fetch telemetry 検証用の理由",
    }])["created"][0]

    result = search_service.get_by_ids([
        {"type": "topic", "id": topic["topic_id"]},
        {"type": "decision", "id": decision["decision_id"]},
    ])
    assert "error" not in result

    _wait_for_telemetry(capture_fetch_telemetry_threads)

    rows = _fetch_all_fetch_telemetry()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "get_by_ids"
    items = json.loads(row["items_json"])
    assert items == [
        {"type": "topic", "id": topic["topic_id"]},
        {"type": "decision", "id": decision["decision_id"]},
    ]
    assert row["timestamp"] is not None
    # caller_session_id 未指定の直接呼出は NULL で記録される
    assert row["caller_session_id"] is None


def test_get_by_ids_empty_items_records_nothing(temp_db):
    """items=[] の no-op 呼出は fetch_telemetry に記録されない"""
    result = search_service.get_by_ids([])
    assert result == {"results": []}
    assert _fetch_all_fetch_telemetry() == []


def test_get_by_ids_too_many_items_records_nothing(temp_db):
    """TOO_MANY_ITEMS エラーになる呼出は fetch_telemetry に記録されない"""
    items = [{"type": "topic", "id": i} for i in range(search_service.GET_BY_IDS_MAX + 1)]

    result = search_service.get_by_ids(items)

    assert result["error"]["code"] == "TOO_MANY_ITEMS"
    assert _fetch_all_fetch_telemetry() == []


def test_get_by_ids_unaffected_when_fetch_telemetry_write_fails(
    temp_db, capture_fetch_telemetry_threads, monkeypatch, caplog
):
    """fetch_telemetry 書込に失敗しても get_by_ids の戻り値は壊れず、警告ログが出る"""
    topic = add_topic(
        title="fetch telemetry 書込失敗フォールバック検証",
        description="例外発生時に get_by_ids 本体が影響を受けないことを検証する",
        tags=DEFAULT_TAGS,
    )

    def flaky_get_connection():
        raise RuntimeError("simulated fetch_telemetry write failure")

    monkeypatch.setattr(search_service, "_telemetry_get_connection", flaky_get_connection)

    with caplog.at_level("WARNING"):
        result = search_service.get_by_ids([{"type": "topic", "id": topic["topic_id"]}])

    _wait_for_telemetry(capture_fetch_telemetry_threads)

    assert "error" not in result
    assert result["results"][0]["type"] == "topic"
    assert any(
        "fetch_telemetry write failed" in record.message for record in caplog.records
    ), f"warning log が出ていない: {[r.message for r in caplog.records]}"


def test_search_records_caller_session_id(temp_db, capture_telemetry_threads):
    """search() に渡した caller_session_id が search_telemetry に記録される"""
    add_topic(
        title="相関キー記録テスト用トピック",
        description="caller_session_id が telemetry に載ることを検証する",
        tags=DEFAULT_TAGS,
    )

    search_service.search(keyword="相関キー記録テスト", caller_session_id="sess-search-1")

    _wait_for_telemetry(capture_telemetry_threads)

    rows = _fetch_all_telemetry()
    assert len(rows) == 1
    assert rows[0]["caller_session_id"] == "sess-search-1"


def test_get_by_ids_records_caller_session_id(temp_db, capture_fetch_telemetry_threads):
    """get_by_ids() に渡した caller_session_id が fetch_telemetry に記録される"""
    topic = add_topic(
        title="fetch 相関キー記録テスト",
        description="caller_session_id が fetch_telemetry に載ることを検証する",
        tags=DEFAULT_TAGS,
    )

    result = search_service.get_by_ids(
        [{"type": "topic", "id": topic["topic_id"]}],
        caller_session_id="sess-fetch-1",
    )
    assert "error" not in result

    _wait_for_telemetry(capture_fetch_telemetry_threads)

    rows = _fetch_all_fetch_telemetry()
    assert len(rows) == 1
    assert rows[0]["caller_session_id"] == "sess-fetch-1"


def _make_search_context(**overrides):
    """`_build_diagnostics` 単体テスト用の最小 SearchContext を組み立てる。"""
    defaults = dict(
        keywords=("foo",),
        fts_keywords=("foo",),
        original_keyword_count=None,
        tag_ids=None,
        entity_type=None,
        limit=10,
        offset=0,
        fetch_limit=30,
        keyword_mode="and",
        include_details=False,
        date_after=None,
        date_before=None,
        domain=None,
    )
    defaults.update(overrides)
    return search_service.SearchContext(**defaults)


def test_build_diagnostics_vector_and_tag_hits():
    """ベクトル検索有効時 vec_hits は実際の整数、tag ヒット時 tag_hits>0、
    methods_used に vector / tag_like が含まれる"""
    ctx = _make_search_context()
    retrieval = {
        "fts": [{"type": "topic", "id": 1}],
        "vec": [{"type": "topic", "id": 1}, {"type": "decision", "id": 2}],
        "tag": [{"type": "log", "id": 3}],
        "methods_used": ["fts5", "vector", "tag_like"],
    }
    weights = search_service._compute_adaptive_weights(1, 2)
    diag = search_service._build_diagnostics(ctx, retrieval, weights)

    assert diag["fts_hits"] == 1
    assert diag["vec_hits"] == 2  # 有効時はヒット数の整数（None ではない）
    assert diag["tag_hits"] == 1  # tag ヒットあり
    assert "vector" in diag["methods_used"]
    assert "tag_like" in diag["methods_used"]
    assert diag["qe_expansions"] == []
    assert diag["adaptive_weights"] == {"w_fts": weights[0], "w_vec": weights[1]}
    assert diag["degraded"] is False  # vec が None でないため


def test_build_diagnostics_vec_hits_zero_when_enabled_but_no_hits():
    """ベクトル検索有効かつヒット 0 件のとき vec_hits は 0（None と区別される）"""
    ctx = _make_search_context()
    retrieval = {
        "fts": [{"type": "topic", "id": 1}],
        "vec": [],  # 有効だがヒットなし
        "tag": [],
        "methods_used": ["fts5", "vector"],
    }
    weights = search_service._compute_adaptive_weights(1, 0)
    diag = search_service._build_diagnostics(ctx, retrieval, weights)

    assert diag["vec_hits"] == 0
    assert diag["tag_hits"] == 0
    assert "vector" in diag["methods_used"]
    assert diag["degraded"] is False  # ヒット0件でも vec は None ではない


def test_build_diagnostics_qe_expansions_populated():
    """Query Expansion 発火時 qe_expansions が拡張分キーワードだけを持つ"""
    ctx = _make_search_context(
        keywords=("foo",),
        fts_keywords=("foo", "bar", "baz"),
        original_keyword_count=1,  # 元 1 件 + 拡張 2 件
    )
    retrieval = {
        "fts": [],
        "vec": None,  # ベクトル検索無効
        "tag": [],
        "methods_used": ["fts5"],
    }
    weights = search_service._compute_adaptive_weights(0, 0)
    diag = search_service._build_diagnostics(ctx, retrieval, weights)

    assert diag["qe_expansions"] == ["bar", "baz"]  # 元キーワードは含まない
    assert diag["vec_hits"] is None  # 無効時は None
    assert diag["degraded"] is True  # vec_hits is None と等価
