"""議論ログ管理サービス"""
import re
import sqlite3
from typing import Optional
from src.db import get_connection, row_to_dict
from src.services.alphaization import alphaize_result_dict_inplace
from src.services.embedding_service import build_embedding_text, generate_and_store_embedding
from src.services.tag_service import (
    validate_and_parse_tags,
    ensure_tag_ids,
    link_tags,
    get_effective_tags_batch,
    get_effective_tags_batch_by_ids,
)


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

                # ログをINSERT
                cursor = conn.execute(
                    "INSERT INTO discussion_logs (topic_id, title, content) VALUES (?, ?, ?)",
                    (topic_id, title, content),
                )
                log_id = cursor.lastrowid

                # タグをリンク（指定された場合のみ）
                if parsed_tags:
                    tag_ids = ensure_tag_ids(conn, parsed_tags)
                    link_tags(conn, "log_tags", "log_id", log_id, tag_ids)

                conn.execute(f"RELEASE SAVEPOINT item_{i}")
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
        議論ログ一覧（各logにtags付き）
        entity_type == "topic": 従来通りtopic_idで直接取得
        entity_type == "activity": related topics（上限10件）経由でlogs集約
    """
    retract_filter = "" if include_retracted else " AND retracted_at IS NULL"

    conn = get_connection()
    try:
        # limitを30件に制限
        limit = min(limit, 30)

        if entity_type == "topic":
            topic_id = entity_id
            if start_id is None:
                rows = conn.execute(
                    f"""
                    SELECT * FROM discussion_logs
                    WHERE topic_id = ?{retract_filter}
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (topic_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM discussion_logs
                    WHERE topic_id = ? AND id >= ?{retract_filter}
                    ORDER BY created_at ASC, id ASC
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
                    "topic_id": log["topic_id"],
                    "title": display_title,
                    "content": log["content"],
                    "tags": tags_map.get(log["id"], []),
                    "created_at": log["created_at"],
                }
                if log.get("retracted_at"):
                    item["retracted_at"] = log["retracted_at"]
                alphaize_result_dict_inplace(item, "log")
                logs.append(item)

            return {"logs": logs}

        elif entity_type == "activity":
            # activity → related topics（上限10件）→ logs集約
            relation_rows = conn.execute(
                "SELECT target_type, target_id FROM relations_view WHERE source_type = ? AND source_id = ?",
                ("activity", entity_id),
            ).fetchall()
            topic_ids = [r["target_id"] for r in relation_rows if r["target_type"] == "topic"][:10]

            if not topic_ids:
                return {"logs": []}

            placeholders = ",".join("?" * len(topic_ids))
            if start_id is None:
                rows = conn.execute(
                    f"""
                    SELECT * FROM discussion_logs
                    WHERE topic_id IN ({placeholders}){retract_filter}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    tuple(topic_ids) + (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM discussion_logs
                    WHERE topic_id IN ({placeholders}) AND id <= ?{retract_filter}
                    ORDER BY id DESC
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
                    "topic_id": log["topic_id"],
                    "title": display_title,
                    "content": log["content"],
                    "tags": tags_map.get(log["id"], []),
                    "created_at": log["created_at"],
                }
                if log.get("retracted_at"):
                    item["retracted_at"] = log["retracted_at"]
                alphaize_result_dict_inplace(item, "log")
                logs.append(item)

            return {"logs": logs}

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
