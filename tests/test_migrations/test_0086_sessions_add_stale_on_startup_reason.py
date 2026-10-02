"""migration 0086_sessions_add_stale_on_startup_reason のテスト

0086適用前後で以下が成立することを検証する:

- 適用前: ended_reasonのCHECK制約は'unregister'/'ttl'/'superseded'のみを許可し、
  'stale_on_startup'はIntegrityErrorで拒否される
- 適用後: 'stale_on_startup'が許可される（既存の3値も引き続き許可される）
- 適用前に投入した既存行が、適用後も内容を保ったまま残る
- idx_sessions_live / idx_sessions_cli_live が再作成されている
"""
import sqlite3

import pytest

from src.db import get_connection, init_database
from test_migrations.conftest import db_before_migration, index_names


@pytest.fixture
def db_before_0086():
    with db_before_migration("0086") as db_path:
        yield db_path


def test_rejects_new_reason_before_migration(db_before_0086):
    conn = get_connection(load_vec=False)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions (session_id, id_kind, ended_at, ended_reason) "
                "VALUES ('before-1', 'bridge', CURRENT_TIMESTAMP, 'stale_on_startup')"
            )
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


def test_accepts_new_reason_and_preserves_existing_rows_after_migration(db_before_0086):
    # 適用前の状態で既存行を1件投入する（旧3値のうちの1つ）
    conn = get_connection(load_vec=False)
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, id_kind, harness, ended_at, ended_reason) "
            "VALUES ('pre-existing', 'bridge', 'claude_code', CURRENT_TIMESTAMP, 'ttl')"
        )
        conn.commit()
    finally:
        conn.close()

    init_database()  # 0086（および以降の未適用分）を適用する

    conn = get_connection(load_vec=False)
    try:
        # 既存行が保たれている
        row = conn.execute(
            "SELECT harness, ended_reason FROM sessions WHERE session_id = 'pre-existing'"
        ).fetchone()
        assert row is not None
        assert dict(row) == {"harness": "claude_code", "ended_reason": "ttl"}

        # 新しい値が許可される
        conn.execute(
            "INSERT INTO sessions (session_id, id_kind, ended_at, ended_reason) "
            "VALUES ('after-1', 'bridge', CURRENT_TIMESTAMP, 'stale_on_startup')"
        )
        conn.commit()
        assert conn.execute(
            "SELECT ended_reason FROM sessions WHERE session_id = 'after-1'"
        ).fetchone()["ended_reason"] == "stale_on_startup"

        # 無効な値は引き続き拒否される
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions (session_id, id_kind, ended_at, ended_reason) "
                "VALUES ('after-2', 'bridge', CURRENT_TIMESTAMP, 'bogus')"
            )
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


def test_indexes_recreated_after_migration(db_before_0086):
    init_database()

    conn = get_connection(load_vec=False)
    try:
        names = index_names(conn, "idx_sessions_%")
    finally:
        conn.close()

    assert names == {"idx_sessions_live", "idx_sessions_cli_live"}
