"""議論ログ管理サービス"""
import re
import sqlite3
from typing import Optional
from src.db import get_connection, row_to_dict
from src.services.citations_service import upsert_citations_for_owner_with_conn
from src.services.readable_id import apply_readable_id_inplace
from src.services.embedding_service import build_embedding_text, generate_and_store_embedding
from src.services.tag_service import (
    validate_and_parse_tags,
    ensure_tag_ids,
    link_tags,
    get_effective_tags_batch,
    get_effective_tags_batch_by_ids,
)
from src.services.relation_service import _add_relation_with_conn


def _auto_generate_title(content: str) -> str | None:
    """contentの先頭行からtitleを自動生成する。生成できない場合はNoneを返す。"""
    first_line = re.split(r'\n|\\n', content.strip(), maxsplit=1)[0].strip()
    title = first_line[:50] if len(first_line) > 50 else first_line
    return title if title else None


def add_logs(items: list[dict]) -> dict:
    """
    複数のログを一括追加する（最大10件）。

    SAVEPOINT方式で各アイテムを個別に処理し、部分成功を許容する。
    embedding生成はcreated分のみ一括で行う。

    Args:
        items: ログ情報のリスト。各要素は以下のキーを持つ:
            - topic_id (int, 必須): 対象トピックのID
            - content (str, 必須): 議論内容（マークダウン可）
            - title (str, optional): ログのタイトル。省略時はcontentの先頭行から自動生成
            - tags (list[str], optional): 追加タグ。省略時はtopicのタグを継承

    Returns:
        {created: [...], errors: [{index, error}]}
    """
    # バリデーション: 1 <= len(items) <= 10
    if not items:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "items must not be empty",
            }
        }
    if len(items) > 10:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "items must not exceed 10",
            }
        }

    created = []
    errors = []

    conn = get_connection()
    try:
        for i, item in enumerate(items):
            conn.execute(f"SAVEPOINT item_{i}")
            try:
                topic_id = item.get("topic_id")
                content = item.get("content", "")
                title = item.get("title")
                tags = item.get("tags")

                # title自動生成
                if not title or not title.strip():
                    title = _auto_generate_title(content)
                    if not title:
                        raise ValueError("title and content cannot both be empty")

                # タグのバリデーション（tagsが指定された場合のみ）
                parsed_tags = None
                if tags is not None:
                    parsed_tags = validate_and_parse_tags(tags)
                    if isinstance(parsed_tags, dict):
                        raise ValueError(parsed_tags["error"]["message"])

                # 親 topic の存在チェック (旧 FK 制約相当の不変条件を維持)
                if topic_id is not None:
                    exists = conn.execute(
                        "SELECT 1 FROM discussion_topics WHERE id = ?",
                        (topic_id,),
                    ).fetchone()
                    if not exists:
                        raise sqlite3.IntegrityError(
                            f"topic_id {topic_id} does not exist in discussion_topics"
                        )

                # ログをINSERT (親 topic は relations.belongs_to で表現するため topic_id は持たせない)
                cursor = conn.execute(
                    "INSERT INTO discussion_logs (title, content) VALUES (?, ?)",
                    (title, content),
                )
                log_id = cursor.lastrowid

                # 親 topic との belongs_to リレーションを記録
                if topic_id is not None:
                    _add_relation_with_conn(
                        conn, "log", log_id,
                        [{"type": "topic", "ids": [topic_id]}],
                    )

                # タグをリンク（指定された場合のみ）
                if parsed_tags:
                    tag_ids = ensure_tag_ids(conn, parsed_tags)
                    link_tags(conn, "log_tags", "log_id", log_id, tag_ids)

                # 本文中の {{cite:X#NNN}} を citations テーブルに保存
                upsert_citations_for_owner_with_conn(
                    conn, "log", log_id, content=content
                )

                conn.execute(f"RELEASE SAVEPOINT item_{i}")
                # topic_id は API 互換のため返す (DB カラムは 0047 で物理削除済み、
                # 親 topic 情報は relations.belongs_to が正)
                created.append({
                    "log_id": log_id,
                    "topic_id": topic_id,
                    "title": title,
                    "content": content,
                })

            except Exception as e:
                conn.execute(f"ROLLBACK TO SAVEPOINT item_{i}")
                conn.execute(f"RELEASE SAVEPOINT item_{i}")
                error_code = "CONSTRAINT_VIOLATION" if isinstance(e, sqlite3.IntegrityError) else "ITEM_ERROR"
                errors.append({
                    "index": i,
                    "error": {"code": error_code, "message": str(e)},
                })

        conn.commit()

        # created分の有効タグを一括取得
        if created:
            created_ids = [c["log_id"] for c in created]
            tags_map = get_effective_tags_batch_by_ids(conn, "log", created_ids)

            # created_atを一括取得
            placeholders = ",".join("?" * len(created_ids))
            rows = conn.execute(
                f"SELECT id, created_at FROM discussion_logs WHERE id IN ({placeholders})",
                tuple(created_ids),
            ).fetchall()
            created_at_map = {row["id"]: row["created_at"] for row in rows}

            for c in created:
                c["tags"] = tags_map.get(c["log_id"], [])
                c["created_at"] = created_at_map.get(c["log_id"])

            # embedding一括生成（created分のみ。失敗してもエラーにしない）
            for c in created:
                tag_text = " ".join(c["tags"]) if c["tags"] else ""
                generate_and_store_embedding(
                    "log", c["log_id"],
                    build_embedding_text(c["title"], c["content"], tag_text),
                )

            # レスポンス軽量化: embedding生成後にcontentを除去
            for c in created:
                c.pop("content", None)

        return {"created": created, "errors": errors}

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


def _count_logs_for_topics(
    conn: sqlite3.Connection,
    topic_ids: list[int],
    log_retract_filter: str,
    id_bound: Optional[tuple[str, int]] = None,
) -> int:
    """topic_ids にbelongs_toするlog件数（DISTINCTで重複除外）を返す。

    id_bound=None なら topic 全体の総件数（start_id/limit の影響を受けない）。
    id_bound=(op, value) を渡すと `l.id op value` の範囲制約を追加する（op は内部
    生成の ">=" / "<=" リテラルのみ）。ページの残件数算出に使う。
    decision_service._count_decisions_for_topics と対称のヘルパー。
    """
    if not topic_ids:
        return 0
    placeholders = ",".join("?" * len(topic_ids))
    params: list[int] = list(topic_ids)
    bound_clause = ""
    if id_bound is not None:
        op, value = id_bound
        bound_clause = f" AND l.id {op} ?"
        params.append(value)
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT l.id) AS cnt FROM discussion_logs l
        JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                        AND r.target_id IN ({placeholders})
        WHERE 1=1{log_retract_filter}{bound_clause}
        """,
        tuple(params),
    ).fetchone()
    return row["cnt"] if row else 0


def get_logs(
    entity_type: str,
    entity_id: int,
    start_id: Optional[int] = None,
    limit: int = 30,
    include_retracted: bool = False,
) -> dict:
    """
    指定エンティティの議論ログを取得する。

    Args:
        entity_type: エンティティタイプ（"topic" または "activity"）
        entity_id: 対象エンティティのID
        start_id: 取得開始位置のログID（ページネーション用）
        limit: 取得件数上限（最大30件）
        include_retracted: Trueのとき取り消し済みログも含める（デフォルトFalse）

    Returns:
        議論ログ一覧（各logにtags付き）。
        entity_type == "topic" のとき、各 item は要求された topic_id を `topic_id` フィールドで返す。
        entity_type == "activity" のとき、各 item は `topic_id` フィールドを含まない
        (複数 topic に belongs_to する場合に「主たる親」を一意に決められないため、
         呼び出し側で必要なら relations.belongs_to を別途 query する設計)。
        total_count: 対象 topic 全体の log 総件数（retractフィルタ適用後、limit/start_idの影響を受けない）
        truncated: この応答が limit/start_id により後続の log を打ち切ったとき true
            （＝続きのページが存在する）
    """
    retract_filter = "" if include_retracted else " AND retracted_at IS NULL"

    conn = get_connection()
    try:
        # limitを30件に制限
        limit = min(limit, 30)

        if entity_type == "topic":
            topic_id = entity_id
            include_topic_id = topic_id
            # discussion_logs の親 topic は relations.belongs_to 経由で解決
            log_retract_filter = retract_filter.replace("retracted_at", "l.retracted_at")
            if start_id is None:
                rows = conn.execute(
                    f"""
                    SELECT l.* FROM discussion_logs l
                    JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                                    AND r.target_type = 'topic' AND r.target_id = ?
                                    AND r.relation_type = 'belongs_to'
                    WHERE 1=1{log_retract_filter}
                    ORDER BY l.created_at ASC, l.id ASC
                    LIMIT ?
                    """,
                    (topic_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT l.* FROM discussion_logs l
                    JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                                    AND r.target_type = 'topic' AND r.target_id = ?
                                    AND r.relation_type = 'belongs_to'
                    WHERE l.id >= ?{log_retract_filter}
                    ORDER BY l.created_at ASC, l.id ASC
                    LIMIT ?
                    """,
                    (topic_id, start_id, limit),
                ).fetchall()

            # バッチでタグ取得
            tags_map = get_effective_tags_batch(conn, "log", topic_id)

            logs = []
            for row in rows:
                log = row_to_dict(row)
                # title fallback: title が空なら content の先頭 50 文字
                display_title = log["title"] or (log["content"] or "")[:50]
                item = {
                    "id": log["id"],
                    "topic_id": include_topic_id,
                    "title": display_title,
                    "content": log["content"],
                    "tags": tags_map.get(log["id"], []),
                    "created_at": log["created_at"],
                }
                if log.get("retracted_at"):
                    item["retracted_at"] = log["retracted_at"]
                apply_readable_id_inplace(item, "log")
                logs.append(item)

            total_count = _count_logs_for_topics(conn, [topic_id], log_retract_filter)
            if start_id is None:
                remaining_count = total_count
            else:
                remaining_count = _count_logs_for_topics(
                    conn, [topic_id], log_retract_filter, id_bound=(">=", start_id)
                )

            return {
                "logs": logs,
                "total_count": total_count,
                "truncated": len(logs) < remaining_count,
            }

        elif entity_type == "activity":
            # activity → related topics（上限10件）→ logs集約
            relation_rows = conn.execute(
                "SELECT target_type, target_id FROM relations_view WHERE source_type = ? AND source_id = ?",
                ("activity", entity_id),
            ).fetchall()
            topic_ids = [r["target_id"] for r in relation_rows if r["target_type"] == "topic"][:10]

            if not topic_ids:
                return {"logs": [], "total_count": 0, "truncated": False}

            placeholders = ",".join("?" * len(topic_ids))
            log_retract_filter = retract_filter.replace("retracted_at", "l.retracted_at")
            if start_id is None:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT l.* FROM discussion_logs l
                    JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                                    AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                                    AND r.target_id IN ({placeholders})
                    WHERE 1=1{log_retract_filter}
                    ORDER BY l.id DESC
                    LIMIT ?
                    """,
                    tuple(topic_ids) + (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT l.* FROM discussion_logs l
                    JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                                    AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                                    AND r.target_id IN ({placeholders})
                    WHERE l.id <= ?{log_retract_filter}
                    ORDER BY l.id DESC
                    LIMIT ?
                    """,
                    tuple(topic_ids) + (start_id, limit),
                ).fetchall()

            # 全topic_idを横断してバッチでタグ取得
            log_ids = [row_to_dict(row)["id"] for row in rows]
            tags_map = get_effective_tags_batch_by_ids(conn, "log", log_ids) if log_ids else {}

            logs = []
            for row in rows:
                log = row_to_dict(row)
                # title fallback: title が空なら content の先頭 50 文字
                display_title = log["title"] or (log["content"] or "")[:50]
                item = {
                    "id": log["id"],
                    "title": display_title,
                    "content": log["content"],
                    "tags": tags_map.get(log["id"], []),
                    "created_at": log["created_at"],
                }
                if log.get("retracted_at"):
                    item["retracted_at"] = log["retracted_at"]
                apply_readable_id_inplace(item, "log")
                logs.append(item)

            total_count = _count_logs_for_topics(conn, topic_ids, log_retract_filter)
            if start_id is None:
                remaining_count = total_count
            else:
                remaining_count = _count_logs_for_topics(
                    conn, topic_ids, log_retract_filter, id_bound=("<=", start_id)
                )

            return {
                "logs": logs,
                "total_count": total_count,
                "truncated": len(logs) < remaining_count,
            }

        else:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"Invalid entity_type: {entity_type}. Must be 'topic' or 'activity'",
                }
            }

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
