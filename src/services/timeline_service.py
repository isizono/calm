"""タイムラインサービス

トピックまたはアクティビティに紐づくdecision・log・materialを時系列で返す。
"""
import logging
from datetime import datetime

from src.db import get_connection
from src.services.readable_id import strip_entity_id_inplace

logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES = {"decision", "log", "material"}
MAX_LIMIT = 100


def get_timeline(
    topic_id: int | None = None,
    activity_id: int | None = None,
    entity_types: list[str] | None = None,
    before: str | None = None,
    limit: int = 50,
    order: str = "desc",
) -> dict:
    """トピックまたはアクティビティに紐づくdecision・log・materialを時系列で返す。

    topic_idまたはactivity_idのいずれか一方を必須で指定する（排他）。
    activity_id指定時はtopic_activity_relationsから関連topic_idsを取得し、
    それらのtopic_idsに紐づくエンティティを集約する。

    Args:
        topic_id: トピックID（activity_idと排他）
        activity_id: アクティビティID（topic_idと排他）
        entity_types: 取得するエンティティ型のリスト（"decision","log","material"のサブセット、未指定で全型）
        before: ページネーション用カーソル（ISO 8601形式のcreated_at、descでの前方ページネーション用）
        limit: 取得件数上限（デフォルト50、最大100）
        order: ソート方向（"desc"または"asc"、デフォルト"desc"）

    Note:
        beforeはdesc順での前方ページネーション用。asc順で次ページを取得するには
        未対応（afterパラメータが必要だが現時点では未実装）。

    Returns:
        {items: [{id, type, title, created_at, replaces, replaced_by}], total}
        totalはbefore条件に関係なく、entity_types・topic_id条件に合致する全件数を返す。
    """
    # --- バリデーション ---

    # topic_id / activity_id 排他チェック
    if topic_id is not None and activity_id is not None:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "topic_id and activity_id are mutually exclusive",
            }
        }
    if topic_id is None and activity_id is None:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Either topic_id or activity_id is required",
            }
        }

    # entity_types バリデーション
    if entity_types is not None:
        if not entity_types:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "entity_types must not be empty when specified",
                }
            }
        invalid = set(entity_types) - VALID_ENTITY_TYPES
        if invalid:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"Invalid entity_types: {sorted(invalid)}. Valid types: {sorted(VALID_ENTITY_TYPES)}",
                }
            }

    # before バリデーション
    if before is not None:
        try:
            datetime.fromisoformat(before)
        except (ValueError, TypeError):
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"Invalid before value: '{before}'. Must be ISO 8601 format (e.g. '2025-01-01 00:00:00')",
                }
            }

    # order バリデーション
    if order not in ("asc", "desc"):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"Invalid order: '{order}'. Must be 'asc' or 'desc'",
            }
        }

    # limit クランプ
    if limit < 1:
        limit = 1
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT

    # 取得対象の型を決定
    types = set(entity_types) if entity_types else VALID_ENTITY_TYPES

    conn = get_connection()
    try:
        # --- topic_ids の解決 ---
        if activity_id is not None:
            rows = conn.execute(
                "SELECT target_id FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'topic' AND relation_type = 'belongs_to'",
                (activity_id,),
            ).fetchall()
            topic_ids = [row["target_id"] for row in rows]
            if not topic_ids:
                return {"items": [], "total": 0}
        else:
            topic_ids = [topic_id]

        placeholders = ",".join("?" * len(topic_ids))

        # --- UNION ALL クエリ構築 ---
        union_parts = []
        params: list = []
        count_parts = []
        count_params: list = []

        if "log" in types:
            union_parts.append(
                f"SELECT DISTINCT l.id, 'log' AS type, l.title, l.created_at, NULL AS replaces_id, NULL AS replaced_by_id"
                f" FROM discussion_logs l"
                f" JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id"
                f"                 AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'"
                f"                 AND r.target_id IN ({placeholders})"
                f" WHERE l.retracted_at IS NULL"
            )
            params.extend(topic_ids)
            count_parts.append(
                f"SELECT DISTINCT l.id, l.created_at"
                f" FROM discussion_logs l"
                f" JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id"
                f"                 AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'"
                f"                 AND r.target_id IN ({placeholders})"
                f" WHERE l.retracted_at IS NULL"
            )
            count_params.extend(topic_ids)

        if "decision" in types:
            # supersedes関係はスカラー（1:1前提）で返す。
            # decision_supersedesは多対多のスキーマだが、APIレスポンスのreplaces/replaced_byは
            # {type, id}のスカラーと定義されているため、最新の1件のみ返す。
            # created_atが同一秒で並んだ場合は選択列のid降順で決定的に1件へ絞る
            # （最新superseder判定を supersede_service 側と一致させる）。
            union_parts.append(
                f"SELECT DISTINCT d.id, 'decision' AS type, COALESCE(d.title, d.decision) AS title, d.created_at,"
                f" (SELECT target_id FROM decision_supersedes WHERE source_id = d.id ORDER BY created_at DESC, target_id DESC LIMIT 1) AS replaces_id,"
                f" (SELECT source_id FROM decision_supersedes WHERE target_id = d.id ORDER BY created_at DESC, source_id DESC LIMIT 1) AS replaced_by_id"
                f" FROM decisions d"
                f" JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id"
                f"                 AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'"
                f"                 AND r.target_id IN ({placeholders})"
                f" WHERE d.retracted_at IS NULL"
            )
            params.extend(topic_ids)
            count_parts.append(
                f"SELECT DISTINCT d.id, d.created_at"
                f" FROM decisions d"
                f" JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id"
                f"                 AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'"
                f"                 AND r.target_id IN ({placeholders})"
                f" WHERE d.retracted_at IS NULL"
            )
            count_params.extend(topic_ids)

        if "material" in types:
            union_parts.append(
                f"SELECT DISTINCT m.id, 'material' AS type, m.title, m.created_at, NULL AS replaces_id, NULL AS replaced_by_id FROM materials m JOIN relations r ON r.source_type = 'material' AND r.source_id = m.id AND r.target_type = 'topic' AND r.relation_type = 'belongs_to' AND r.target_id IN ({placeholders}) WHERE m.retracted_at IS NULL"
            )
            params.extend(topic_ids)
            count_parts.append(
                f"SELECT DISTINCT m.id, m.created_at FROM materials m JOIN relations r ON r.source_type = 'material' AND r.source_id = m.id AND r.target_type = 'topic' AND r.relation_type = 'belongs_to' AND r.target_id IN ({placeholders}) WHERE m.retracted_at IS NULL"
            )
            count_params.extend(topic_ids)

        # before カーソル条件を外側のWHEREで適用
        base_query = " UNION ALL ".join(union_parts)

        # totalは常にbefore条件なしの全件数を返す
        count_query = f"SELECT COUNT(*) FROM ({' UNION ALL '.join(count_parts)}) AS c"

        if before:
            query = f"SELECT id, type, title, created_at, replaces_id, replaced_by_id FROM ({base_query}) AS t WHERE t.created_at < ? ORDER BY t.created_at {order} LIMIT ?"
            params.append(before)
            params.append(limit)
        else:
            query = f"SELECT id, type, title, created_at, replaces_id, replaced_by_id FROM ({base_query}) AS t ORDER BY t.created_at {order} LIMIT ?"
            params.append(limit)

        # --- クエリ実行 ---
        rows = conn.execute(query, params).fetchall()
        total_row = conn.execute(count_query, count_params).fetchone()
        total = total_row[0] if total_row else 0

        items = []
        for row in rows:
            replaces = None
            if row["replaces_id"]:
                replaces = {"type": "decision", "id": row["replaces_id"]}
                # replaces 内には title が無いので id_raw 退避のみ（title=None）
                strip_entity_id_inplace(replaces)
            replaced_by = None
            if row["replaced_by_id"]:
                replaced_by = {"type": "decision", "id": row["replaced_by_id"]}
                strip_entity_id_inplace(replaced_by)

            item = {
                "id": row["id"],
                "type": row["type"],
                "title": row["title"],
                "created_at": row["created_at"],
                "replaces": replaces,
                "replaced_by": replaced_by,
            }
            strip_entity_id_inplace(item)
            items.append(item)

        return {"items": items, "total": total}

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
