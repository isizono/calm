"""エンティティの取り消し（retract）管理サービス"""
import logging
import sqlite3
from datetime import datetime, timezone

from src.db import get_connection
from src.services import embedding_service
from src.services.relay.entity_publish import publish_entity_event_with_conn

logger = logging.getLogger(__name__)

ENTITY_TABLE_MAP = {
    "decision": "decisions",
    "log": "discussion_logs",
    "material": "materials",
}

# search_indexのsource_typeとENTITY_TABLE_MAPの対応
_SEARCH_INDEX_SOURCE_TYPE = {
    "decision": "decision",
    "log": "log",
    "material": "material",
}

# FTS5 contentless 'delete' コマンドはインサート時と同じtitle/body値を要求するため、
# 元テーブルからbodyカラム相当を取り直すクエリ（INSERTトリガーと整合させる必要あり）。
# - decision: trg_search_decisions_insert → VALUES (last_insert_rowid(), NEW.decision, NEW.reason)
# - log:      trg_search_logs_insert      → VALUES (last_insert_rowid(), NEW.title, NEW.content)
# - material: trg_search_materials_insert → VALUES (last_insert_rowid(), NEW.title, NEW.content)
_FTS_BODY_QUERY = {
    "decision": "SELECT reason FROM decisions WHERE id = ?",
    "log": "SELECT content FROM discussion_logs WHERE id = ?",
    "material": "SELECT content FROM materials WHERE id = ?",
}


def _delete_search_index_entry(conn, source_type: str, source_id: int) -> None:
    """search_index / search_index_fts / vec_index から該当エントリを物理削除する。

    contentless FTS5は通常のDELETE FROM search_index_fts WHERE ... ができないため、
    'delete'コマンドINSERTでマーカー消去する。SQLite公式仕様により、'delete'コマンドには
    インサート時と同じtitle/body値を渡す必要がある（異なる値を渡すとインデックスが
    unknown stateになり肥大化・BM25スコア歪みの原因となる）。

    エントリが存在しない場合は何もしない（冪等）。
    呼び出し側がトランザクション/SAVEPOINTを管理する。
    """
    row = conn.execute(
        "SELECT id, title FROM search_index WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    ).fetchone()
    if not row:
        return

    search_index_id = row["id"]
    title = row["title"] or ""

    body = ""
    body_query = _FTS_BODY_QUERY.get(source_type)
    if body_query:
        body_row = conn.execute(body_query, (source_id,)).fetchone()
        if body_row is not None:
            body = body_row[0] or ""

    conn.execute(
        "INSERT INTO search_index_fts (search_index_fts, rowid, title, body) VALUES ('delete', ?, ?, ?)",
        (search_index_id, title, body),
    )
    embedding_service.delete_embedding_with_conn(conn, search_index_id)
    conn.execute("DELETE FROM search_index WHERE id = ?", (search_index_id,))


def retract(entity_type: str, ids: list[int], undo: bool = False) -> dict:
    """エンティティを取り消し（retract）またはun-retractする。

    SAVEPOINT方式で各IDを個別処理し、部分成功を許容する。
    冪等: 既にretracted状態でretractしても成功扱い、
    既に非retracted状態でun-retractしても成功扱い。

    retract時はretracted_atをUPDATEした直後に、search_index / search_index_fts /
    vec_index からも物理削除する（同じSAVEPOINT内）。これによりsearch経路の
    NOT EXISTS二段フィルタが不要になり、KNNの実効recall劣化も解消する。

    undo時はretracted_atをNULLに戻すのみで、search経路への再登録は行わない。
    物理削除は不可逆として扱う。un-retractしたエンティティを再び検索可能にしたい
    場合は、別途add_decisions/add_logs/add_materialで新規追加する。

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
                    # retract: retracted_at IS NULLの場合のみ更新 + search index物理削除
                    if row["retracted_at"] is None:
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute(
                            f"UPDATE {table} SET retracted_at = ? WHERE id = ?",
                            (now, entity_id),
                        )
                        _delete_search_index_entry(
                            conn, _SEARCH_INDEX_SOURCE_TYPE[entity_type], entity_id
                        )
                        publish_entity_event_with_conn(
                            conn, entity_type=entity_type, entity_id=entity_id, event="retracted"
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
