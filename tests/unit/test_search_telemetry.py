"""search_telemetry テーブル書込のテスト

検証項目:
1. migration 0041 後に search_telemetry テーブルが存在する (id / query / parameters /
   result_count / timestamp カラムを持つ)
2. search() を呼ぶと search_telemetry に行が追加され、query / parameters / result_count
   が期待する JSON / 整数で記録される
3. 書込は別スレッドで行われる (`_record_search_telemetry_async` が Thread を返す)
4. 書込中に例外が発生しても search() の戻り値は通常通りで、本体を壊さない
"""
import json
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services import search_service
from src.services.topic_service import add_topic
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


def _wait_for_telemetry(threads, timeout=5.0):
    for t in threads:
        if t is not None:
            t.join(timeout=timeout)


def _fetch_all_telemetry():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, query, parameters, result_count, timestamp "
            "FROM search_telemetry ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_search_telemetry_table_schema(temp_db):
    """migration 0041 で search_telemetry テーブルと必要なカラムが作られる"""
    conn = get_connection()
    try:
        rows = conn.execute("PRAGMA table_info(search_telemetry)").fetchall()
    finally:
        conn.close()
    columns = {r["name"]: r for r in rows}
    assert {"id", "query", "parameters", "result_count", "timestamp"}.issubset(columns)
    assert columns["query"]["notnull"] == 1
    assert columns["parameters"]["notnull"] == 1
    assert columns["result_count"]["notnull"] == 1
    assert columns["timestamp"]["notnull"] == 1


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
