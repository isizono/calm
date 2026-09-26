"""test_migrations 共通のスキーマ検査ユーティリティ。

migration テストで頻出する「カラム名一覧」「テーブル存在確認」「インデックス名一覧」を
sqlite_master / PRAGMA から取得する純粋ヘルパーを提供する。
"""
import atexit
import contextlib
import os
import shutil
import sqlite3
import tempfile

from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend

# migration_id -> 「migration_id未満を適用済み」のテンプレートDBパス。
# 同じmigration_idを対象とするテストは同一ファイル内に数件〜数十件並ぶが、
# 毎回ゼロから数十本のmigrationを適用すると1件あたり数百ms〜数秒かかる
# (yoyoがmigrationごとにsqlparseでSQL分割・ホスト名解決・commitを行うため)。
# テンプレートはプロセス(xdistワーカー)内で1回だけ構築し、以後はファイルコピーで配る。
_TEMPLATE_CACHE: dict[str, str] = {}
_TEMPLATE_DIR: str | None = None


def _template_dir() -> str:
    global _TEMPLATE_DIR
    if _TEMPLATE_DIR is None:
        _TEMPLATE_DIR = tempfile.mkdtemp(prefix="migration_templates_")
        atexit.register(shutil.rmtree, _TEMPLATE_DIR, ignore_errors=True)
    return _TEMPLATE_DIR


def _build_template(migration_id: str) -> str:
    """migration_id未満のmigrationのみを適用したテンプレートDBを構築してパスを返す。"""
    db_path = os.path.join(_template_dir(), f"before_{migration_id}.db")
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    backend.init_database()
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    pre = MigrationList([m for m in all_migs if m.id < migration_id])
    with backend.lock():
        backend.apply_migrations(pre)
    backend.connection.close()
    # WALモードで残った差分をメインファイルへ戻し、単一ファイルコピーで完結させる
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return db_path


@contextlib.contextmanager
def db_before_migration(migration_id: str):
    """migration_id未満のmigrationのみを適用したテスト用DBを提供する。

    各test_00XX.pyのdb_before_00XX()フィクスチャが、対象migration IDを渡して呼び出す。
    テストごとに独立したコピーを返すため、テスト間でDB状態は共有されない。
    """
    template = _TEMPLATE_CACHE.get(migration_id)
    if template is None:
        template = _build_template(migration_id)
        _TEMPLATE_CACHE[migration_id] = template
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        shutil.copyfile(template, db_path)
        os.environ["DISCUSSION_DB_PATH"] = db_path
        try:
            yield db_path
        finally:
            os.environ.pop("DISCUSSION_DB_PATH", None)


def get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """指定テーブルが sqlite_master に存在するか確認する。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def index_names(conn: sqlite3.Connection, name_pattern: str) -> set[str]:
    """sqlite_master から name LIKE pattern のインデックス名セットを返す。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
        (name_pattern,),
    ).fetchall()
    return {row["name"] for row in rows}
