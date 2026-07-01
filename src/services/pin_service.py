"""エンティティのpin管理サービス"""
import logging
import sqlite3
from typing import Optional, Union

from src.db import get_connection
from src.services.supersede_service import get_superseded_by_batch
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


def _is_decision_superseded(conn: sqlite3.Connection, decision_id: int) -> Optional[int]:
    """decisionがsupersedeされていれば、supersederのdecision_id（1件）を返す。

    複数のsupersederがある場合は最新の1件を返す（最新判定の tie-break は
    get_superseded_by_batch に委譲して全経路で一致させる）。
    superseded されていなければ None。
    """
    return get_superseded_by_batch(conn, [decision_id])[decision_id]


def _transfer_pins_with_conn(
    conn: sqlite3.Connection,
    entity_type: str,
    old_id: int,
    new_id: int,
) -> int:
    """旧entityへのpinを新entityへ付け替える（target側 + source側の両方向）。

    INSERT OR IGNORE + DELETE の2文方式（両方向で計4文）。
    - UNIQUE衝突（new側に既存pinあり）はOR IGNOREでマージ → 旧行DELETEで消滅
    - 自己参照化するpin（旧⇔新間のpin）はWHERE除外＋DELETEで消滅させる
    - 新pinの created_at は旧pinの値を引き継ぐ

    Args:
        conn: DB接続（呼び出し元のトランザクションに参加）
        entity_type: 付け替え対象entityの種別（supersedes用途では現状 "decision"）
        old_id: 付け替え元entityのID
        new_id: 付け替え先entityのID

    Returns:
        実際にINSERTされたpin件数。
        衝突マージ（OR IGNOREで除かれた行）と自己参照化で消滅した行はカウントしない。
    """
    # target側付け替え: pin(X, old) → pin(X, new)
    # 自己参照化(pin(new, old) → pin(new, new))は WHERE で除外して移動を止め、
    # 旧行は後段の DELETE で消滅させる
    target_cursor = conn.execute(
        "INSERT OR IGNORE INTO pins (source_type, source_id, target_type, target_id, created_at) "
        "SELECT source_type, source_id, ?, ?, created_at "
        "FROM pins "
        "WHERE target_type = ? AND target_id = ? "
        "AND NOT (source_type = ? AND source_id = ?)",
        (entity_type, new_id, entity_type, old_id, entity_type, new_id),
    )
    target_inserted = target_cursor.rowcount
    conn.execute(
        "DELETE FROM pins WHERE target_type = ? AND target_id = ?",
        (entity_type, old_id),
    )

    # source側付け替え: pin(old, Y) → pin(new, Y)
    # 自己参照化(pin(old, new) → pin(new, new))は WHERE で除外
    source_cursor = conn.execute(
        "INSERT OR IGNORE INTO pins (source_type, source_id, target_type, target_id, created_at) "
        "SELECT ?, ?, target_type, target_id, created_at "
        "FROM pins "
        "WHERE source_type = ? AND source_id = ? "
        "AND NOT (target_type = ? AND target_id = ?)",
        (entity_type, new_id, entity_type, old_id, entity_type, new_id),
    )
    source_inserted = source_cursor.rowcount
    conn.execute(
        "DELETE FROM pins WHERE source_type = ? AND source_id = ?",
        (entity_type, old_id),
    )

    return target_inserted + source_inserted


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

        result = {
            "source_type": source_type,
            "source_id": source_id,
            "target_type": target_type,
            "target_id": target_id,
        }

        # supersededへのpinはhintを付与する（自動解決はしない）。
        # hintクエリはcommit済みpinへの補足情報。失敗してもpin挿入は維持しhintなしで返す。
        try:
            hints = []
            if source_type == "decision":
                superseder_id = _is_decision_superseded(conn, source_id)
                if superseder_id is not None:
                    hints.append(
                        f"decision#{source_id} は decision#{superseder_id} に superseded されています。"
                        f"古い decision を意図的に pin する場合はそのままで問題ありません。"
                    )
            if target_type == "decision":
                superseder_id = _is_decision_superseded(conn, target_id)
                if superseder_id is not None:
                    hints.append(
                        f"decision#{target_id} は decision#{superseder_id} に superseded されています。"
                        f"古い decision を意図的に pin する場合はそのままで問題ありません。"
                    )
            if hints:
                result["hint"] = "\n".join(hints)
        except Exception:
            pass

        return result

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
