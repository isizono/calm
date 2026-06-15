"""エンティティのpin管理サービス"""
import logging
import sqlite3
from typing import Optional, Union

from src.db import get_connection
from src.services.tag_service import parse_tag, resolve_tag_ids

logger = logging.getLogger(__name__)

ENTITY_TABLE_MAP = {
    "decision": "decisions",
    "log": "discussion_logs",
    "material": "materials",
    "topic": "discussion_topics",
    "activity": "activities",
    "tag": "tags",
}


def _resolve_ref(
    conn: sqlite3.Connection, entity_type: str, ref: Union[int, str]
) -> tuple[Optional[int], Optional[dict]]:
    """entity_type と ref から entity_id を解決する。

    tag かつ str の場合は parse_tag → resolve_tag_ids で解決する。
    解決できなかった tag str は (None, None) を返し、呼び出し側で意味付けする
    （add_pin → NOT_FOUND、remove_pin → 冪等な {"removed": 0} 短絡）。
    int キャスト失敗のみエラーとして返す。

    Returns:
        解決成功: (entity_id, None)
        tag str 未存在: (None, None)
        int 変換失敗: (None, {"error": {"code": "VALIDATION_ERROR", ...}})
    """
    if entity_type == "tag" and isinstance(ref, str):
        parsed = parse_tag(ref)
        ids = resolve_tag_ids(conn, [parsed])
        return (ids[0] if ids else None, None)
    try:
        return (int(ref), None)
    except (ValueError, TypeError):
        return (None, {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"ref must be an integer for entity_type '{entity_type}', got: {ref!r}",
            }
        })


def add_pin(
    source_type: str,
    source_ref: Union[int, str],
    target_type: str,
    target_ref: Union[int, str],
) -> dict:
    """pinを追加する（source → target）。

    Args:
        source_type: 起点エンティティ種別
        source_ref: 起点エンティティのID（intまたはstr）。tagのみnamespace:name形式を許容
        target_type: 終点エンティティ種別
        target_ref: 終点エンティティのID（intまたはstr）。tagのみnamespace:name形式を許容

    Returns:
        成功時: {"source_type": str, "source_id": int, "target_type": str, "target_id": int}
        失敗時: {"error": {"code": str, "message": str}}
    """
    # source_type / target_type の検証
    if source_type not in ENTITY_TABLE_MAP:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid source_type: {source_type}. Must be one of: {', '.join(sorted(ENTITY_TABLE_MAP.keys()))}",
            }
        }
    if target_type not in ENTITY_TABLE_MAP:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid target_type: {target_type}. Must be one of: {', '.join(sorted(ENTITY_TABLE_MAP.keys()))}",
            }
        }

    conn = get_connection()
    try:
        # ref解決
        source_id, err = _resolve_ref(conn, source_type, source_ref)
        if err:
            return err
        if source_id is None:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"tag '{source_ref}' not found",
                }
            }

        target_id, err = _resolve_ref(conn, target_type, target_ref)
        if err:
            return err
        if target_id is None:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"tag '{target_ref}' not found",
                }
            }

        # 自己参照拒否
        if source_type == target_type and source_id == target_id:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"Self-reference is not allowed: {source_type}#{source_id} → {target_type}#{target_id}",
                }
            }

        # 存在性チェック
        source_table = ENTITY_TABLE_MAP[source_type]
        row = conn.execute(
            f"SELECT id FROM {source_table} WHERE id = ?", (source_id,)
        ).fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"{source_type} with id {source_id} not found",
                }
            }

        target_table = ENTITY_TABLE_MAP[target_type]
        row = conn.execute(
            f"SELECT id FROM {target_table} WHERE id = ?", (target_id,)
        ).fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"{target_type} with id {target_id} not found",
                }
            }

        # INSERT OR IGNORE（冪等）
        conn.execute(
            "INSERT OR IGNORE INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
            (source_type, source_id, target_type, target_id),
        )
        conn.commit()

        return {
            "source_type": source_type,
            "source_id": source_id,
            "target_type": target_type,
            "target_id": target_id,
        }

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


def remove_pin(
    source_type: str,
    source_ref: Union[int, str],
    target_type: str,
    target_ref: Union[int, str],
) -> dict:
    """pinを削除する（source → target）。

    Args:
        source_type: 起点エンティティ種別
        source_ref: 起点エンティティのID（intまたはstr）。tagのみnamespace:name形式を許容
        target_type: 終点エンティティ種別
        target_ref: 終点エンティティのID（intまたはstr）。tagのみnamespace:name形式を許容

    Returns:
        成功時: {"removed": int}（実際に削除された件数）
        失敗時: {"error": {"code": str, "message": str}}
    """
    # source_type / target_type の検証
    if source_type not in ENTITY_TABLE_MAP:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid source_type: {source_type}. Must be one of: {', '.join(sorted(ENTITY_TABLE_MAP.keys()))}",
            }
        }
    if target_type not in ENTITY_TABLE_MAP:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid target_type: {target_type}. Must be one of: {', '.join(sorted(ENTITY_TABLE_MAP.keys()))}",
            }
        }

    conn = get_connection()
    try:
        # ref解決。tag str 未存在は冪等な {"removed": 0} で短絡
        source_id, err = _resolve_ref(conn, source_type, source_ref)
        if err:
            return err
        if source_id is None:
            return {"removed": 0}

        target_id, err = _resolve_ref(conn, target_type, target_ref)
        if err:
            return err
        if target_id is None:
            return {"removed": 0}

        # DELETE実行（pinsテーブルに外部キー制約はないためIntegrityErrorは発生しない）
        cursor = conn.execute(
            "DELETE FROM pins WHERE source_type=? AND source_id=? AND target_type=? AND target_id=?",
            (source_type, source_id, target_type, target_id),
        )
        conn.commit()

        return {"removed": cursor.rowcount}

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
