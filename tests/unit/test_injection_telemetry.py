"""injection_telemetry（記録=クエリ添付の追随カウンタ）テーブル書込のテスト

検証項目:
1. migration 0067 後に injection_telemetry テーブルが存在する（必要なカラムを持つ）
2. injection_telemetry.timestamp / attached の index が張られている
3. `_record_injection_telemetry_async` が attachments の要素数だけ present 行を書く
4. attachments=[] のときは何も書かず空リストを返す（第3層添付が未実装/ゼロ件のケース）
5. 未知カラムを payload に混ぜると `_record_telemetry_async` の assert で開発時に落ちる
6. 書込中に例外が発生しても呼出元は例外を受け取らず、警告ログが出る（既存 telemetry と同じ握りつぶし規約）
7. get_material 呼出が fetch_telemetry に tool='get_material' として記録される
   （`search_service.record_material_fetch_telemetry` 経由の計装）
"""
import json
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services import search_service
from src.services.material_service import add_material


DEFAULT_TAGS = ["domain:test"]


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
def capture_injection_telemetry_threads(monkeypatch):
    """_record_injection_telemetry_async が起動した thread 群を捕捉して join() できるようにする"""
    threads = []
    original = search_service._record_injection_telemetry_async

    def wrapped(*args, **kwargs):
        started = original(*args, **kwargs)
        threads.extend(started)
        return started

    monkeypatch.setattr(search_service, "_record_injection_telemetry_async", wrapped)
    return threads


@pytest.fixture
def capture_fetch_telemetry_threads(monkeypatch):
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


def _fetch_all_injection_telemetry():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, caller_session_id, trigger_tool, source_type, source_id, "
            "attached_type, attached_id, rank, similarity, diagnostics_json, timestamp "
            "FROM injection_telemetry ORDER BY id"
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


def test_injection_telemetry_table_schema(temp_db):
    """migration 0067 で injection_telemetry テーブルと必要なカラムが作られる"""
    conn = get_connection()
    try:
        rows = conn.execute("PRAGMA table_info(injection_telemetry)").fetchall()
    finally:
        conn.close()
    columns = {r["name"]: r for r in rows}
    assert {
        "id", "caller_session_id", "trigger_tool", "source_type", "source_id",
        "attached_type", "attached_id", "rank", "similarity", "diagnostics_json",
        "timestamp",
    }.issubset(columns)
    assert columns["trigger_tool"]["notnull"] == 1
    assert columns["source_type"]["notnull"] == 1
    assert columns["source_id"]["notnull"] == 1
    assert columns["attached_type"]["notnull"] == 1
    assert columns["attached_id"]["notnull"] == 1
    assert columns["rank"]["notnull"] == 1
    assert columns["timestamp"]["notnull"] == 1
    # caller_session_id / similarity / diagnostics_json は NULL 許容
    assert columns["caller_session_id"]["notnull"] == 0
    assert columns["similarity"]["notnull"] == 0
    assert columns["diagnostics_json"]["notnull"] == 0


def test_injection_telemetry_indexes(temp_db):
    """session/timestamp と attached 用の index が張られている"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='injection_telemetry'"
        ).fetchall()
    finally:
        conn.close()
    index_names = {r["name"] for r in rows}
    assert "idx_injection_telemetry_session_ts" in index_names
    assert "idx_injection_telemetry_attached" in index_names


def test_record_injection_telemetry_writes_one_row_per_attachment(
    temp_db, capture_injection_telemetry_threads
):
    """attachments の各要素につき injection_telemetry に1行ずつ present が書かれる"""
    attachments = [
        {"type": "decision", "id": 3195, "rank": 1, "similarity": 0.9, "diagnostics": None},
        {"type": "decision", "id": 3182, "rank": 2, "similarity": 0.7, "diagnostics": {"src": "fts"}},
    ]

    threads = search_service._record_injection_telemetry_async(
        trigger_tool="add_decisions",
        source_type="decision",
        source_id=3210,
        attachments=attachments,
        caller_session_id="sess-inject-1",
    )
    assert len(threads) == 1

    _wait_for_telemetry(capture_injection_telemetry_threads)

    rows = _fetch_all_injection_telemetry()
    assert len(rows) == 2

    row1, row2 = rows
    assert row1["trigger_tool"] == "add_decisions"
    assert row1["source_type"] == "decision"
    assert row1["source_id"] == 3210
    assert row1["attached_type"] == "decision"
    assert row1["attached_id"] == 3195
    assert row1["rank"] == 1
    assert row1["similarity"] == pytest.approx(0.9)
    assert row1["diagnostics_json"] is None
    assert row1["caller_session_id"] == "sess-inject-1"
    assert row1["timestamp"] is not None

    assert row2["attached_id"] == 3182
    assert row2["rank"] == 2
    assert json.loads(row2["diagnostics_json"]) == {"src": "fts"}


def test_record_injection_telemetry_empty_attachments_writes_nothing(temp_db):
    """attachments=[] のときは書込ゼロ件・空リストを返す"""
    threads = search_service._record_injection_telemetry_async(
        trigger_tool="add_material",
        source_type="material",
        source_id=1,
        attachments=[],
    )
    assert threads == []
    assert _fetch_all_injection_telemetry() == []


def test_record_injection_telemetry_caller_session_id_null_when_unspecified(
    temp_db, capture_injection_telemetry_threads
):
    """caller_session_id 未指定の呼出は NULL で記録される（MCP context 外の呼出想定）"""
    search_service._record_injection_telemetry_async(
        trigger_tool="add_logs",
        source_type="log",
        source_id=5,
        attachments=[{"type": "log", "id": 6, "rank": 1, "similarity": None, "diagnostics": None}],
    )

    _wait_for_telemetry(capture_injection_telemetry_threads)

    rows = _fetch_all_injection_telemetry()
    assert len(rows) == 1
    assert rows[0]["caller_session_id"] is None


def test_injection_telemetry_unknown_column_rejected():
    """allowlist に無いカラムを混ぜると _record_telemetry_async が assert で拒否する"""
    with pytest.raises(AssertionError):
        search_service._record_telemetry_async(
            "injection_telemetry",
            {"trigger_tool": "add_logs", "not_a_real_column": "x"},
        )


def test_injection_telemetry_unaffected_when_write_fails(
    temp_db, capture_injection_telemetry_threads, monkeypatch, caplog
):
    """書込中に例外が出ても呼出元へ例外は伝播せず、警告ログが出る"""

    def flaky_get_connection():
        raise RuntimeError("simulated injection_telemetry write failure")

    monkeypatch.setattr(search_service, "_telemetry_get_connection", flaky_get_connection)

    with caplog.at_level("WARNING"):
        threads = search_service._record_injection_telemetry_async(
            trigger_tool="add_decisions",
            source_type="decision",
            source_id=1,
            attachments=[{"type": "decision", "id": 2, "rank": 1, "similarity": None, "diagnostics": None}],
        )

    _wait_for_telemetry(threads)

    assert len(threads) == 1
    assert _fetch_all_injection_telemetry() == []
    assert any(
        "injection_telemetry write failed" in record.message for record in caplog.records
    ), f"warning log が出ていない: {[r.message for r in caplog.records]}"


def test_injection_telemetry_unaffected_when_attachment_malformed(
    temp_db, capture_injection_telemetry_threads, caplog
):
    """attachments の要素にキー欠落（例: "rank" なし）があっても呼出元へ例外は伝播しない

    dict-literal 組立時（呼出元スレッド）ではなく書込スレッド側で `.get()` により
    アクセスするため、"rank" 欠落は NOT NULL 制約違反として書込失敗になるだけで
    KeyError が呼出元スレッドへ伝播しない（既存の書込失敗テストとは別の
    「payload 組立時」の経路を検証する）。
    """
    attachments = [
        {"type": "decision", "id": 42, "similarity": None, "diagnostics": None},  # "rank" 欠落
    ]

    with caplog.at_level("WARNING"):
        threads = search_service._record_injection_telemetry_async(
            trigger_tool="add_decisions",
            source_type="decision",
            source_id=1,
            attachments=attachments,
            caller_session_id="sess-inject-malformed",
        )

    assert len(threads) == 1
    _wait_for_telemetry(capture_injection_telemetry_threads)

    assert _fetch_all_injection_telemetry() == []
    assert any(
        "injection_telemetry write failed" in record.message for record in caplog.records
    ), f"warning log が出ていない: {[r.message for r in caplog.records]}"


def test_record_material_fetch_telemetry_writes_get_material_row(
    temp_db, capture_fetch_telemetry_threads
):
    """record_material_fetch_telemetry() が fetch_telemetry に tool='get_material' の行を書く"""
    material = add_material(
        title="fetch telemetry 計装検証用資材",
        content="get_material 経由の fetch_telemetry 計装を検証する",
        tags=DEFAULT_TAGS,
        source="test",
    )

    thread = search_service.record_material_fetch_telemetry(
        material["material_id"], caller_session_id="sess-material-1"
    )

    _wait_for_telemetry([thread])

    rows = _fetch_all_fetch_telemetry()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "get_material"
    assert json.loads(row["items_json"]) == [{"type": "material", "id": material["material_id"]}]
    assert row["caller_session_id"] == "sess-material-1"


def test_get_material_tool_records_fetch_telemetry(
    temp_db, capture_fetch_telemetry_threads, monkeypatch
):
    """main.get_material ツール呼出が fetch_telemetry に get_material として記録される"""
    import src.main as main_module

    monkeypatch.setattr(main_module, "_current_session_id", lambda: "sess-get-material-tool")

    material = add_material(
        title="get_materialツール計装検証",
        content="main.get_material 経由でも fetch_telemetry が書かれることを検証する",
        tags=DEFAULT_TAGS,
        source="test",
    )

    result = main_module.get_material(material["material_id"])
    assert "error" not in result

    _wait_for_telemetry(capture_fetch_telemetry_threads)

    rows = _fetch_all_fetch_telemetry()
    assert len(rows) == 1
    assert rows[0]["tool"] == "get_material"
    assert json.loads(rows[0]["items_json"]) == [
        {"type": "material", "id": material["material_id"]}
    ]
    assert rows[0]["caller_session_id"] == "sess-get-material-tool"


def test_get_material_unaffected_when_fetch_telemetry_write_fails(
    temp_db, capture_fetch_telemetry_threads, monkeypatch, caplog
):
    """fetch_telemetry 書込が失敗しても main.get_material の戻り値は壊れない"""
    import src.main as main_module

    material = add_material(
        title="get_materialツール書込失敗フォールバック検証",
        content="fetch_telemetry 書込失敗時にget_material本体が影響を受けないことを検証する",
        tags=DEFAULT_TAGS,
        source="test",
    )

    def flaky_get_connection():
        raise RuntimeError("simulated fetch_telemetry write failure")

    monkeypatch.setattr(search_service, "_telemetry_get_connection", flaky_get_connection)

    with caplog.at_level("WARNING"):
        result = main_module.get_material(material["material_id"])

    _wait_for_telemetry(capture_fetch_telemetry_threads)

    assert "error" not in result
    assert result["content"] == "fetch_telemetry 書込失敗時にget_material本体が影響を受けないことを検証する"
    assert any(
        "fetch_telemetry write failed" in record.message for record in caplog.records
    ), f"warning log が出ていない: {[r.message for r in caplog.records]}"
