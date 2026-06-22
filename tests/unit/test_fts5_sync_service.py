"""fts5_sync_service の DDL 生成・install 経路テスト。

検証対象:
- 各 spec の render_* が現行 migration 最終形の DDL と意味的に等価
- install() で drop→create した後の挙動が既存トリガーと同等 (INSERT/UPDATE/DELETE 同期)
- install_all() の冪等性
- spec の識別子バリデーション
"""

import os
import re
import sqlite3
import tempfile

import pytest

import src.services.embedding_service as emb
from src.db import init_database
from src.services.fts5_sync_service import (
    FTS5_SPECS,
    Fts5SyncSpec,
    install,
    install_all,
    render_create_triggers,
    render_delete_trigger,
    render_drop_triggers,
    render_insert_trigger,
    render_update_trigger,
)


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
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


def _normalize_sql(sql: str) -> str:
    """空白を圧縮して比較しやすくする。CREATE TRIGGER の本体比較用。

    SQLite は sqlite_master.sql に保存するときに `IF NOT EXISTS` を剥がして格納するため、
    比較時にもこのキーワードを除去して揃える。
    """
    collapsed = re.sub(r"\s+", " ", sql).strip()
    return re.sub(r"\bIF NOT EXISTS\s+", "", collapsed)


def _trigger_sql_from_db(db_path: str, trigger_name: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ============================
# DDL 生成 (render_*) 単体テスト
# ============================


def test_render_insert_trigger_topic():
    spec = FTS5_SPECS[0]  # topic
    sql = render_insert_trigger(spec)
    assert "CREATE TRIGGER IF NOT EXISTS trg_search_topics_insert" in sql
    assert "AFTER INSERT ON discussion_topics" in sql
    assert "INSERT INTO search_index (source_type, source_id, title, created_at)" in sql
    assert "'topic', NEW.id, NEW.title, NEW.created_at" in sql
    assert "INSERT INTO search_index_fts (rowid, title, body)" in sql
    assert "NEW.title, NEW.description" in sql


def test_render_insert_trigger_decision_uses_coalesce_for_display_title():
    spec = next(s for s in FTS5_SPECS if s.source_type == "decision")
    sql = render_insert_trigger(spec)
    # display title (search_index.title) は COALESCE
    assert "COALESCE(NEW.title, NEW.decision), NEW.created_at" in sql
    # FTS title/body は decision/reason
    assert "NEW.decision, NEW.reason" in sql


def test_render_update_trigger_decision_uses_coalesce():
    spec = next(s for s in FTS5_SPECS if s.source_type == "decision")
    sql = render_update_trigger(spec)
    assert "SET title = COALESCE(NEW.title, NEW.decision)" in sql
    # 'delete' marker は OLD.decision/OLD.reason
    assert "OLD.decision, OLD.reason" in sql
    assert "NEW.decision, NEW.reason" in sql


def test_render_delete_trigger_material():
    spec = next(s for s in FTS5_SPECS if s.source_type == "material")
    sql = render_delete_trigger(spec)
    assert "AFTER DELETE ON materials" in sql
    assert "DELETE FROM search_index WHERE source_type = 'material'" in sql
    assert "OLD.title, OLD.content" in sql


def test_render_create_triggers_returns_three():
    spec = FTS5_SPECS[0]
    triggers = render_create_triggers(spec)
    assert len(triggers) == 3
    assert "AFTER INSERT" in triggers[0]
    assert "AFTER UPDATE" in triggers[1]
    assert "AFTER DELETE" in triggers[2]


def test_render_drop_triggers_returns_three_names():
    spec = next(s for s in FTS5_SPECS if s.source_type == "log")
    drops = render_drop_triggers(spec)
    assert drops == (
        "DROP TRIGGER IF EXISTS trg_search_logs_insert",
        "DROP TRIGGER IF EXISTS trg_search_logs_update",
        "DROP TRIGGER IF EXISTS trg_search_logs_delete",
    )


# ============================
# spec バリデーション
# ============================


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_type": "bad name", "src_table": "t", "trigger_basename": "ts", "fts_title_column": "a", "fts_body_column": "b"},
        {"source_type": "x", "src_table": "1bad", "trigger_basename": "ts", "fts_title_column": "a", "fts_body_column": "b"},
        {"source_type": "x", "src_table": "t", "trigger_basename": "ts", "fts_title_column": "a;DROP", "fts_body_column": "b"},
        {"source_type": "x", "src_table": "t", "trigger_basename": "ts", "fts_title_column": "a", "fts_body_column": "b'c"},
        {"source_type": "x", "src_table": "t", "trigger_basename": "bad name", "fts_title_column": "a", "fts_body_column": "b"},
    ],
)
def test_invalid_identifier_rejected(kwargs):
    with pytest.raises(ValueError):
        Fts5SyncSpec(**kwargs)


@pytest.mark.parametrize(
    "expr",
    [
        "NEW.title; DROP TABLE search_index",  # SQL injection 形
        "(SELECT 'x')",  # 任意の SQL 式
        "OLD.title",  # OLD は不可
        "COALESCE(NEW.title)",  # 引数 1 個は許容しない (COALESCE の意味がない)
        "COALESCE(NEW.title, 'literal')",  # NEW.<ident> 以外の引数
        "coalesce(NEW.title, NEW.decision)",  # 小文字 (大文字のみ許容)
        "NEW.title || NEW.decision",  # 任意の式
    ],
)
def test_invalid_display_title_expr_rejected(expr):
    with pytest.raises(ValueError):
        Fts5SyncSpec(
            source_type="x",
            src_table="t",
            trigger_basename="ts",
            fts_title_column="a",
            fts_body_column="b",
            display_title_expr=expr,
        )


@pytest.mark.parametrize(
    "expr",
    [
        "NEW.title",
        "NEW.decision",
        "COALESCE(NEW.title, NEW.decision)",
        "COALESCE(NEW.a, NEW.b, NEW.c)",
    ],
)
def test_valid_display_title_expr_accepted(expr):
    spec = Fts5SyncSpec(
        source_type="x",
        src_table="t",
        trigger_basename="ts",
        fts_title_column="a",
        fts_body_column="b",
        display_title_expr=expr,
    )
    assert spec.display_title_expr == expr


# ============================
# Parity: 生成 DDL が現行 migration 最終形と等価
# ============================


@pytest.mark.parametrize("spec", FTS5_SPECS, ids=[s.source_type for s in FTS5_SPECS])
def test_parity_with_existing_migrations(temp_db, spec):
    """init_database() 適用後の DB に存在する各 src_type の insert/update/delete トリガー
    DDL が、render_* の出力と空白正規化後に一致することを確認する。

    既存 migration を一切変更せずに新レイヤが「同じ DDL」を吐けることを担保する。
    """
    for trigger_name, rendered_sql in (
        (spec.trigger_insert_name, render_insert_trigger(spec)),
        (spec.trigger_update_name, render_update_trigger(spec)),
        (spec.trigger_delete_name, render_delete_trigger(spec)),
    ):
        existing_sql = _trigger_sql_from_db(temp_db, trigger_name)
        assert existing_sql is not None, f"既存 DB に {trigger_name} が存在しない"
        assert _normalize_sql(existing_sql) == _normalize_sql(rendered_sql), (
            f"{trigger_name} の生成 DDL が migration の最終形と一致しない\n"
            f"existing: {existing_sql}\nrendered: {rendered_sql}"
        )


def test_install_re_emits_same_ddl(temp_db):
    """install() で drop→create した後の sqlite_master.sql が render 出力と一致する。"""
    for spec in FTS5_SPECS:
        conn = sqlite3.connect(temp_db)
        try:
            install(conn, spec)
            conn.commit()
        finally:
            conn.close()

        for trigger_name, rendered_sql in (
            (spec.trigger_insert_name, render_insert_trigger(spec)),
            (spec.trigger_update_name, render_update_trigger(spec)),
            (spec.trigger_delete_name, render_delete_trigger(spec)),
        ):
            stored = _trigger_sql_from_db(temp_db, trigger_name)
            assert stored is not None
            assert _normalize_sql(stored) == _normalize_sql(rendered_sql)


def test_install_all_idempotent(temp_db):
    """install_all() を 2 回呼んでもエラーにならず、最終状態が変わらないこと。"""
    conn = sqlite3.connect(temp_db)
    try:
        install_all(conn)
        conn.commit()
        install_all(conn)
        conn.commit()
    finally:
        conn.close()

    for spec in FTS5_SPECS:
        for trigger_name in (
            spec.trigger_insert_name,
            spec.trigger_update_name,
            spec.trigger_delete_name,
        ):
            assert _trigger_sql_from_db(temp_db, trigger_name) is not None


# ============================
# Runtime parity: install 後の同期挙動
# ============================


def test_runtime_sync_topic_after_install(temp_db):
    """install 後でも discussion_topics の INSERT/UPDATE/DELETE で
    search_index / search_index_fts が同期されることを確認する。
    """
    spec = next(s for s in FTS5_SPECS if s.source_type == "topic")
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        install(conn, spec)
        conn.execute(
            "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
            ("ユニーク見出し abcfooxyz", "本文 quxbarzzz"),
        )
        topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # search_index 同期
        si_row = conn.execute(
            "SELECT * FROM search_index WHERE source_type='topic' AND source_id=?",
            (topic_id,),
        ).fetchone()
        assert si_row is not None
        assert si_row["title"] == "ユニーク見出し abcfooxyz"

        # FTS5 検索ヒット (insert)
        hit_title = conn.execute(
            "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
            ("abcfooxyz",),
        ).fetchone()
        assert hit_title is not None
        hit_body = conn.execute(
            "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
            ("quxbarzzz",),
        ).fetchone()
        assert hit_body is not None

        # UPDATE で旧マーカーが消え、新マーカーがヒット
        conn.execute(
            "UPDATE discussion_topics SET title=?, description=? WHERE id=?",
            ("新タイトル qqqfoo123", "新本文 wwwbar456", topic_id),
        )
        # 旧 token は消えている
        old_hit = conn.execute(
            "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
            ("abcfooxyz",),
        ).fetchone()
        assert old_hit is None
        # 新 token はヒット
        new_hit = conn.execute(
            "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
            ("qqqfoo123",),
        ).fetchone()
        assert new_hit is not None
        # display title も追従
        updated_si = conn.execute(
            "SELECT title FROM search_index WHERE source_type='topic' AND source_id=?",
            (topic_id,),
        ).fetchone()
        assert updated_si["title"] == "新タイトル qqqfoo123"

        # DELETE で search_index も search_index_fts も消える
        conn.execute("DELETE FROM discussion_topics WHERE id=?", (topic_id,))
        gone = conn.execute(
            "SELECT * FROM search_index WHERE source_type='topic' AND source_id=?",
            (topic_id,),
        ).fetchone()
        assert gone is None
        no_hit = conn.execute(
            "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
            ("qqqfoo123",),
        ).fetchone()
        assert no_hit is None
    finally:
        conn.close()


def test_runtime_sync_decision_display_title_null(temp_db):
    """decisions.title が NULL のとき search_index.title に decision 本文が入る (COALESCE)。"""
    spec = next(s for s in FTS5_SPECS if s.source_type == "decision")
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        install(conn, spec)
        # decisions に投入するには topic が要る
        conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
        topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO decisions (topic_id, decision, reason, title) VALUES (?, ?, ?, NULL)",
            (topic_id, "決定本文 uniqdectoken", "理由 uniqreasontoken"),
        )
        d_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        si = conn.execute(
            "SELECT title FROM search_index WHERE source_type='decision' AND source_id=?",
            (d_id,),
        ).fetchone()
        # title=NULL なので decision 本文が display title になる
        assert si["title"] == "決定本文 uniqdectoken"
        # FTS body は reason
        body_hit = conn.execute(
            "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
            ("uniqreasontoken",),
        ).fetchone()
        assert body_hit is not None
    finally:
        conn.close()


def test_runtime_sync_decision_display_title_set(temp_db):
    """decisions.title が非 NULL のとき search_index.title に title が入る (COALESCE)。"""
    spec = next(s for s in FTS5_SPECS if s.source_type == "decision")
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        install(conn, spec)
        conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t2', 'd2')")
        topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO decisions (topic_id, decision, reason, title) VALUES (?, ?, ?, ?)",
            (topic_id, "本文タイトルではない", "理由本文", "短いタイトル uniqshort"),
        )
        d_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        si = conn.execute(
            "SELECT title FROM search_index WHERE source_type='decision' AND source_id=?",
            (d_id,),
        ).fetchone()
        assert si["title"] == "短いタイトル uniqshort"
    finally:
        conn.close()


def test_runtime_sync_all_specs_after_install_all(temp_db):
    """install_all() 後でも全 src_type の INSERT が同期される。"""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        install_all(conn)
        # topic
        conn.execute(
            "INSERT INTO discussion_topics (title, description) VALUES ('tp uniqA001', 'dp uniqA002')"
        )
        topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # activity
        conn.execute(
            "INSERT INTO activities (title, description, status) VALUES ('act uniqA003', 'desc uniqA004', 'in_progress')"
        )
        # log (discussion_logs requires topic_id)
        conn.execute(
            "INSERT INTO discussion_logs (topic_id, title, content) VALUES (?, 'log uniqA005', 'cont uniqA006')",
            (topic_id,),
        )
        # material
        conn.execute(
            "INSERT INTO materials (title, content) VALUES ('mat uniqA007', 'cont uniqA008')"
        )
        # decision
        conn.execute(
            "INSERT INTO decisions (topic_id, decision, reason, title) VALUES (?, 'dec uniqA009', 'rea uniqA010', NULL)",
            (topic_id,),
        )

        for token in [
            "uniqA001",  # topic title
            "uniqA002",  # topic body
            "uniqA003",  # activity title
            "uniqA004",  # activity body
            "uniqA005",  # log title
            "uniqA006",  # log body
            "uniqA007",  # material title
            "uniqA008",  # material body
            "uniqA009",  # decision title (= NEW.decision)
            "uniqA010",  # decision body (= NEW.reason)
        ]:
            row = conn.execute(
                "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
                (token,),
            ).fetchone()
            assert row is not None, f"token {token} がヒットしない"
    finally:
        conn.close()
