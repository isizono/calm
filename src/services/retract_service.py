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

# un-retract時の再登録で使う、search_index.title（表示用タイトル）取得クエリ。
# 対応するINSERTトリガーのdisplay_title_exprと一致させる必要あり
# （migrations/0046_relations_belongs_to_unify.sql参照）。decisionのみ
# COALESCE(title, decision)で、search_index_fts.title（下記_FTS_TITLE_QUERY）とは
# 値が異なる非対称構造になっている。
_SEARCH_INDEX_TITLE_QUERY = {
    "decision": "SELECT COALESCE(title, decision) FROM decisions WHERE id = ?",
    "log": "SELECT title FROM discussion_logs WHERE id = ?",
    "material": "SELECT title FROM materials WHERE id = ?",
}

# un-retract時の再登録で使う、search_index_fts.title（FTS検索対象）取得クエリ。
# decisionは表示用titleにCOALESCEせず常にdecision本文（対応するINSERTトリガーの
# NEW.decisionと一致させる）。
_FTS_TITLE_QUERY = {
    "decision": "SELECT decision FROM decisions WHERE id = ?",
    "log": "SELECT title FROM discussion_logs WHERE id = ?",
    "material": "SELECT title FROM materials WHERE id = ?",
}

_CREATED_AT_QUERY = {
    "decision": "SELECT created_at FROM decisions WHERE id = ?",
    "log": "SELECT created_at FROM discussion_logs WHERE id = ?",
    "material": "SELECT created_at FROM materials WHERE id = ?",
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


def _reregister_search_index_with_conn(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> bool:
    """un-retract時、search_index / search_index_fts へ明示的に再登録する（冪等）。

    retractはsearch_index/search_index_ftsを物理削除するため（_delete_search_index_entry
    参照）、un-retractしたエンティティは対応するINSERTトリガー相当の登録をやり直す必要がある。

    この関数を`UPDATE {table} SET retracted_at = NULL`より前に呼ぶこと。理由:
    trg_search_*_updateトリガー（AFTER UPDATE、全カラム対象で無条件発火）は内部で
    `(SELECT id FROM search_index WHERE source_type=? AND source_id=OLD.id)`により
    search_index.idを引き当てる実装になっている。retract済み行（search_index側の
    対応行が既に物理削除されている）に対してこのUPDATEを先に実行すると、サブクエリが
    NULLを返し、`INSERT INTO search_index_fts (rowid, ...) VALUES (NULL, ...)`で
    FTS5がrowidを自動採番してしまう。この自動採番idはsearch_index.idのAUTOINCREMENT
    シーケンス（sqlite_sequence）とは独立に進むため、後から追加される別エンティティの
    search_index.idと衝突しうる（衝突すると、取り消し済みエンティティの本文で検索した
    はずが無関係な別エンティティがヒットする）。
    本関数を先に呼びsearch_index行を正しいidで復元しておけば、直後のUPDATEトリガーは
    そのidを正常に引き当てて自分自身を再同期するだけになり、上記の衝突が起きない。

    既にsearch_index行が存在する場合（retract_service導入前にretractされ、物理削除を
    経ていない古い状態等）は何もしない。

    Returns:
        True: 新規にsearch_index/search_index_ftsへ登録した（呼び出し側はcommit後に
            embeddingの再生成が必要）
        False: 既に存在しており何もしなかった
    """
    existing = conn.execute(
        "SELECT id FROM search_index WHERE source_type = ? AND source_id = ?",
        (_SEARCH_INDEX_SOURCE_TYPE[entity_type], entity_id),
    ).fetchone()
    if existing:
        return False

    display_title = conn.execute(
        _SEARCH_INDEX_TITLE_QUERY[entity_type], (entity_id,)
    ).fetchone()[0]
    fts_title = conn.execute(_FTS_TITLE_QUERY[entity_type], (entity_id,)).fetchone()[0]
    fts_body = conn.execute(_FTS_BODY_QUERY[entity_type], (entity_id,)).fetchone()[0]
    created_at = conn.execute(_CREATED_AT_QUERY[entity_type], (entity_id,)).fetchone()[0]

    cursor = conn.execute(
        "INSERT INTO search_index (source_type, source_id, title, created_at) VALUES (?, ?, ?, ?)",
        (_SEARCH_INDEX_SOURCE_TYPE[entity_type], entity_id, display_title, created_at),
    )
    search_index_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO search_index_fts (rowid, title, body) VALUES (?, ?, ?)",
        (search_index_id, fts_title, fts_body),
    )
    return True


def retract(entity_type: str, ids: list[int], undo: bool = False) -> dict:
    """エンティティを取り消し（retract）またはun-retractする。

    SAVEPOINT方式で各IDを個別処理し、部分成功を許容する。
    冪等: 既にretracted状態でretractしても成功扱い、
    既に非retracted状態でun-retractしても成功扱い。

    retract時はretracted_atをUPDATEした直後に、search_index / search_index_fts /
    vec_index からも物理削除する（同じSAVEPOINT内）。これによりsearch経路の
    NOT EXISTS二段フィルタが不要になり、KNNの実効recall劣化も解消する。

    undo時はretracted_atをNULLに戻すと同時に、search_index / search_index_fts への
    再登録も行う（同じSAVEPOINT内、_reregister_search_index_with_conn参照）。
    vec_indexへの再登録はcommit後にベストエフォートで行う（embedding再生成、
    add_material等と同じ非同期扱い）。

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
    # undo時に新規でsearch_index再登録した(entity_type, entity_id)。commit後に
    # embedding/vec_indexをベストエフォートで再生成する対象（_reregister_search_index_with_conn参照）。
    pending_embedding_regen: list[tuple[str, int]] = []

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
                        # search_index再登録は retracted_at クリアより先に行う
                        # (理由は_reregister_search_index_with_connのdocstring参照)
                        reregistered = _reregister_search_index_with_conn(
                            conn, entity_type, entity_id
                        )
                        conn.execute(
                            f"UPDATE {table} SET retracted_at = NULL WHERE id = ?",
                            (entity_id,),
                        )
                        if reregistered:
                            pending_embedding_regen.append((entity_type, entity_id))
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

        # commit後、search_index再登録したエンティティのembedding/vec_indexを
        # ベストエフォートで再生成する（add_material等と同じ非同期扱い、失敗してもretract結果には影響しない）。
        for regen_entity_type, regen_entity_id in pending_embedding_regen:
            embedding_service.regenerate_embedding(regen_entity_type, regen_entity_id)

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
