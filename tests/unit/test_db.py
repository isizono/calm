"""データベース機能のテスト"""
import os
from pathlib import Path
import pytest
from src.db import get_db_path, get_connection, execute_query, execute_insert



def test_get_db_path_with_env():
    """環境変数が設定されている場合、その値を返す"""
    test_path = "/tmp/test.db"
    os.environ["DISCUSSION_DB_PATH"] = test_path
    try:
        assert get_db_path() == test_path
    finally:
        del os.environ["DISCUSSION_DB_PATH"]


def test_get_db_path_default():
    """環境変数が未設定の場合、デフォルトパスを返す"""
    if "DISCUSSION_DB_PATH" in os.environ:
        del os.environ["DISCUSSION_DB_PATH"]

    path = get_db_path()
    assert path.endswith(".claude-code-memory/discussion.db")


def test_get_db_path_ignores_stale_config_db_path(monkeypatch):
    """src.config.DB_PATHが別の値でも、get_db_path()は現在の環境変数を返す

    src.config.DB_PATHはモジュール初回import時に一度だけ解決される定数。
    get_db_path()がこれをキャッシュとして優先していた旧実装では、
    DB_PATHが一度でも非空値で固定されると、以後の環境変数の変更が
    無視され続けた。
    """
    import src.config as config

    monkeypatch.setattr(config, "DB_PATH", "/tmp/stale-cached-path.db")
    monkeypatch.setenv("DISCUSSION_DB_PATH", "/tmp/current-path.db")

    assert get_db_path() == "/tmp/current-path.db"


def test_init_database(temp_db):
    """データベース初期化が成功する"""
    conn = get_connection()
    try:
        # テーブルが作成されていることを確認
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor.fetchall()]

        assert "discussion_topics" in tables
        assert "discussion_logs" in tables
        assert "decisions" in tables
        assert "tags" in tables
        assert "topic_tags" in tables
        assert "activity_tags" in tables
        assert "decision_tags" in tables
        assert "log_tags" in tables
    finally:
        conn.close()


def test_execute_insert_and_query(temp_db):
    """INSERT と SELECT が正しく動作する"""
    # トピックを追加（subjects廃止後）
    topic_id = execute_insert(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("test-topic", "テストトピック"),
    )
    assert topic_id > 0

    # 追加したトピックを取得
    rows = execute_query("SELECT * FROM discussion_topics WHERE id = ?", (topic_id,))
    assert len(rows) == 1
    assert rows[0]["title"] == "test-topic"
    assert rows[0]["description"] == "テストトピック"


def test_get_connection_returns_row_factory(temp_db):
    """接続が Row factory を使用している"""
    conn = get_connection()
    try:
        # トピックを追加
        conn.execute(
            "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
            ("test-topic", "Test description"),
        )
        conn.commit()

        # Row として取得できることを確認
        cursor = conn.execute("SELECT * FROM discussion_topics WHERE title = 'test-topic'")
        row = cursor.fetchone()
        assert row["title"] == "test-topic"  # 辞書ライクなアクセス
    finally:
        conn.close()
