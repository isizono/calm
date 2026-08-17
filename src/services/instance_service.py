"""cc-memoryインスタンス識別子(instance_id)管理サービス

export/importバンドルの複合キー(`<instance_id>:<型コード><ローカルID>`)生成の
基盤となるインスタンス自身の識別子を管理する。identifierはDB内の単一行テーブル
instance_metaに保持する(DBファイルと運命を共にするため、環境変数には置かない)。
"""
import re
import sqlite3

from src.db import get_connection

__all__ = [
    "INSTANCE_ID_PATTERN",
    "set_instance_identity",
    "get_instance_id",
    "get_instance_id_with_conn",
]

# DNSラベル風: 先頭は英小文字、以降は英小文字・数字・ハイフン、全体で3〜32字。
# 複合キーの区切り文字(':')・型コード(大文字)との衝突を避けるため大文字を禁止する。
INSTANCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,31}$")


def get_instance_id_with_conn(conn: sqlite3.Connection) -> str | None:
    """conn共有版: instance_idを返す(未設定ならNone)。"""
    row = conn.execute("SELECT instance_id FROM instance_meta WHERE id = 1").fetchone()
    return row["instance_id"] if row else None


def get_instance_id() -> str | None:
    """instance_idを返す(未設定ならNone)。"""
    conn = get_connection(load_vec=False)
    try:
        return get_instance_id_with_conn(conn)
    finally:
        conn.close()


def set_instance_identity(instance_id: str, force: bool = False) -> dict:
    """インスタンス識別子を設定する。

    一度設定したら原則変更不可(force無しでは拒否)。複合キーは出生インスタンスの
    識別子を基準に発行されるため、変更は既発行の複合キーの意味を壊す破壊的操作。

    Args:
        instance_id: 設定する識別子。DNSラベル風(`^[a-z][a-z0-9-]{2,31}$`)。
        force: Trueのとき既存の設定を上書きする(デフォルトFalse)。

    Returns:
        成功時: {"instance_id": str, "created_at": str}
        失敗時: {"error": {"code": "VALIDATION_ERROR" | "ALREADY_EXISTS" | "DATABASE_ERROR", "message": str}}
    """
    if not instance_id or not INSTANCE_ID_PATTERN.match(instance_id):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    f"instance_id must match {INSTANCE_ID_PATTERN.pattern!r} "
                    "(lowercase letters, digits, hyphens; 3-32 chars; must start with a letter)"
                ),
            }
        }

    conn = get_connection(load_vec=False)
    try:
        existing = conn.execute(
            "SELECT instance_id FROM instance_meta WHERE id = 1"
        ).fetchone()
        if existing is not None and not force:
            return {
                "error": {
                    "code": "ALREADY_EXISTS",
                    "message": (
                        f"instance_id is already set to '{existing['instance_id']}'. "
                        "Changing it invalidates composite keys already issued under the "
                        "current identity. Pass force=True to override."
                    ),
                }
            }

        if existing is not None:
            conn.execute(
                "UPDATE instance_meta SET instance_id = ?, created_at = datetime('now') WHERE id = 1",
                (instance_id,),
            )
        else:
            conn.execute(
                "INSERT INTO instance_meta (id, instance_id) VALUES (1, ?)",
                (instance_id,),
            )
        conn.commit()

        row = conn.execute(
            "SELECT instance_id, created_at FROM instance_meta WHERE id = 1"
        ).fetchone()
        return {"instance_id": row["instance_id"], "created_at": row["created_at"]}
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()
