"""エンティティの取り消し（retract）管理サービス"""
import logging
import sqlite3
from datetime import datetime, timezone

from src.db import get_connection

logger = logging.getLogger(__name__)

ENTITY_TABLE_MAP = {
    "decision": "decisions",
    "log": "discussion_logs",
    "material": "materials",
}


def retract(entity_type: str, ids: list[int], undo: bool = False) -> dict:
    """エンティティを取り消し（retract）またはun-retractする。

    SAVEPOINT方式で各IDを個別処理し、部分成功を許容する。
    冪等: 既にretracted状態でretractしても成功扱い、
    既に非retracted状態でun-retractしても成功扱い。

    Args:
        entity_type: エンティティ種別 ("decision" | "log" | "material")
        ids: 対象エンティティのIDリスト
        undo: True=un-retract（retracted_atをNULLに戻す）、False=retract

    Returns:
        {success: [int, ...], errors: [{id, error}]}
        またはエラー {"error": {"code": str, "message": str}}
    """
    # entity_type検証
    if entity_type not in ENTITY_TABLE_MAP:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid entity_type: {entity_type}. Must be one of: {', '.join(sorted(ENTITY_TABLE_MAP.keys()))}",
            }
        }

    # ids検証
    if not ids:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "ids must not be empty",
            }
        }

    table = ENTITY_TABLE_MAP[entity_type]
    success = []
    errors = []

    conn = get_connection()
    try:
        for i, entity_id in enumerate(ids):
            conn.execute(f"SAVEPOINT retract_{i}")
            try:
                # 存在確認
                row = conn.execute(
                    f"SELECT id, retracted_at FROM {table} WHERE id = ?",
                    (entity_id,),
                ).fetchone()

                if not row:
                    raise ValueError(f"{entity_type} with id {entity_id} not found")

                if undo:
                    # un-retract: retracted_at IS NOT NULLの場合のみ更新
                    if row["retracted_at"] is not None:
                        conn.execute(
                            f"UPDATE {table} SET retracted_at = NULL WHERE id = ?",
                            (entity_id,),
                        )
                else:
                    # retract: retracted_at IS NULLの場合のみ更新
                    if row["retracted_at"] is None:
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute(
                            f"UPDATE {table} SET retracted_at = ? WHERE id = ?",
                            (now, entity_id),
                        )

                conn.execute(f"RELEASE SAVEPOINT retract_{i}")
                success.append(entity_id)

            except Exception as e:
                conn.execute(f"ROLLBACK TO SAVEPOINT retract_{i}")
                conn.execute(f"RELEASE SAVEPOINT retract_{i}")
                errors.append({
                    "id": entity_id,
                    "error": {"code": "ITEM_ERROR", "message": str(e)},
                })

        conn.commit()
        return {"success": success, "errors": errors}

    except Exception as e:
        conn.rollback()
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
