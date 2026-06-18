"""資材管理サービス"""
import logging
import sqlite3
from typing import Literal

from src.db import get_connection, row_to_dict
from src.services.embedding_service import build_embedding_text, generate_and_store_embedding
from src.services.relation_service import _add_relation_with_conn, _validate_targets
from src.services.tag_service import (
    validate_and_parse_tags,
    ensure_tag_ids,
    link_tags,
    get_entity_tags,
    get_entity_tags_batch,
)

logger = logging.getLogger(__name__)

SNIPPET_MAX_LEN = 200


def _material_to_response(material: dict, tags: list[str]) -> dict:
    """資材データをAPIレスポンス形式に変換（全文含む）"""
    return {
        "material_id": material["id"],
        "title": material["title"],
        "content": material["content"],
        "source": material["source"],
        "tags": tags,
        "created_at": material["created_at"],
        "hint": "contentの先頭1-2文は内容の説明・要約にしてください（check-in時にsnippetとして表示されます）",
    }


def add_material(title: str, content: str, tags: list[str], source: str, related: list[dict] | None = None) -> dict:
    """
    資材を追加する

    Args:
        title: 資材のタイトル
        content: 資材の本文
        tags: タグ配列（必須、1個以上）
        source: データの出自
        related: 関連エンティティ [{"type": "topic", "ids": [1, 2]}, ...] (optional)

    Returns:
        作成された資材情報
    """
    if not title or not title.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "title must not be empty",
            }
        }

    if not content or not content.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "content must not be empty",
            }
        }

    if not source or not source.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "source must not be empty",
            }
        }

    # タグのバリデーション
    parsed_tags = validate_and_parse_tags(tags, required=True)
    if isinstance(parsed_tags, dict):
        return parsed_tags

    # relatedのバリデーション
    if related:
        err = _validate_targets("material", related)
        if err:
            return err

    conn = get_connection()
    try:
        # updated_at は created_at と同値で初期化する（recomposeナッジ判定の基準時刻T用）。
        # created_at の DEFAULT 式に揃え、INSERT内で同一の strftime 値をセットする。
        cursor = conn.execute(
            "INSERT INTO materials (title, content, source, updated_at) "
            "VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', 'now'))",
            (title, content, source),
        )
        material_id = cursor.lastrowid

        # タグをリンク
        tag_ids = ensure_tag_ids(conn, parsed_tags)
        link_tags(conn, "material_tags", "material_id", material_id, tag_ids)

        # リレーションを追加
        if related:
            _add_relation_with_conn(conn, "material", material_id, related)

        # タグを取得（commit前）
        tag_strings = get_entity_tags(conn, "material_tags", "material_id", material_id)

        conn.commit()

        # embedding生成（失敗してもmaterial作成には影響しない）
        tag_text = " ".join(tag_strings) if tag_strings else ""
        generate_and_store_embedding("material", material_id, build_embedding_text(title, content, tag_text))

        return {"material_id": material_id}

    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {
            "error": {
                "code": "CONSTRAINT_VIOLATION",
                "message": str(e),
            }
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


def get_materials_by_relation_with_conn(conn, activity_id: int) -> list[dict]:
    """
    アクティビティにリレーションで紐づく資材一覧をカタログ形式で取得する（conn共有版）

    Args:
        conn: SQLiteコネクション
        activity_id: アクティビティのID

    Returns:
        資材カタログのリスト [{"id": int, "title": str, "snippet": str, "tags": list[str], "created_at": str}, ...]
    """
    rows = conn.execute(
        """SELECT m.id, m.title, m.content, m.source, m.created_at
           FROM materials m
           JOIN relations r ON r.source_type = 'activity' AND r.source_id = ?
                           AND r.target_type = 'material' AND r.target_id = m.id
           ORDER BY m.created_at ASC""",
        (activity_id,),
    ).fetchall()
    material_ids = [row["id"] for row in rows]
    tags_map = get_entity_tags_batch(conn, "material_tags", "material_id", material_ids) if material_ids else {}
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "snippet": (row["content"] or "")[:SNIPPET_MAX_LEN],
            "source": row["source"],
            "tags": tags_map.get(row["id"], []),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


CONTENT_JOIN_SEPARATOR = "\n\n"


def update_material(
    material_id: int,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    mode: Literal["overwrite", "prepend", "append"] = "overwrite",
) -> dict:
    """
    Update an existing material's content, title, and/or tags.

    Args:
        material_id: ID of the material to update
        content: New content (optional)
        title: New title (optional)
        tags: New tags (full replace, optional. At least 1 required when specified)
        source: New source (optional)
        mode: content指定時の結合動作。"overwrite"=既定で上書き、"prepend"=新+区切り+既存、
              "append"=既存+区切り+新。区切りは "\n\n"。既存contentが空文字列の場合はoverwrite相当。
              contentが未指定（None）の場合はmodeは無視される。

    Returns:
        Updated material info
    """
    if content is None and title is None and tags is None and source is None:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "At least one of content, title, tags, or source must be provided",
            }
        }

    # modeのバリデーション（content指定時のみ。content=Noneならmodeは無視されるため）
    if content is not None and mode not in ("overwrite", "prepend", "append"):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"mode must be one of 'overwrite', 'prepend', 'append', got {mode!r}",
            }
        }

    # タグのバリデーション（tags指定時のみ）
    parsed_tags = None
    if tags is not None:
        parsed_tags = validate_and_parse_tags(tags, required=True)
        if isinstance(parsed_tags, dict):
            return parsed_tags

    if title is not None and not title.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "title must not be empty",
            }
        }

    if content is not None and not content.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "content must not be empty",
            }
        }

    if source is not None and not source.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "source must not be empty",
            }
        }

    conn = get_connection()
    try:
        # Check existence
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ?", (material_id,)
        ).fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Material with id {material_id} not found",
                }
            }

        # content の値を mode に応じて結合する
        effective_content = content
        if content is not None and mode != "overwrite":
            existing_content = row["content"] or ""
            if existing_content == "":
                effective_content = content
            elif mode == "prepend":
                effective_content = content + CONTENT_JOIN_SEPARATOR + existing_content
            else:  # append
                effective_content = existing_content + CONTENT_JOIN_SEPARATOR + content

        # Build dynamic SQL for title/content
        set_parts = []
        values = []

        if title is not None:
            set_parts.append("title = ?")
            values.append(title)

        if content is not None:
            set_parts.append("content = ?")
            values.append(effective_content)

        if source is not None:
            set_parts.append("source = ?")
            values.append(source)

        # updated_atは常に現在時刻で更新する（recomposeナッジ判定の基準時刻T用）。
        # title/content/source指定が無くtagsのみ更新するケースでもupdated_atは進める。
        set_parts.append("updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now')")

        set_clause = ", ".join(set_parts)
        values.append(material_id)
        conn.execute(
            f"UPDATE materials SET {set_clause} WHERE id = ?",
            tuple(values),
        )

        # タグの全置換（tags指定時のみ）
        if parsed_tags is not None:
            conn.execute("DELETE FROM material_tags WHERE material_id = ?", (material_id,))
            tag_ids = ensure_tag_ids(conn, parsed_tags)
            link_tags(conn, "material_tags", "material_id", material_id, tag_ids)

        conn.commit()

        # Retrieve updated material
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ?", (material_id,)
        ).fetchone()
        if not row:
            raise Exception("Failed to retrieve updated material")

        # Get tags
        tag_strings = get_entity_tags(conn, "material_tags", "material_id", material_id)

        # Regenerate embedding
        updated = row_to_dict(row)
        tag_text = " ".join(tag_strings) if tag_strings else ""
        generate_and_store_embedding(
            "material", material_id,
            build_embedding_text(updated["title"], updated["content"], tag_text),
        )

        return {"material_id": material_id}

    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {
            "error": {
                "code": "CONSTRAINT_VIOLATION",
                "message": str(e),
            }
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


def get_material(material_id: int) -> dict:
    """
    資材を全文取得する

    Args:
        material_id: 資材のID

    Returns:
        資材の全文情報
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ?", (material_id,)
        ).fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Material with id {material_id} not found",
                }
            }

        # タグを取得
        tag_strings = get_entity_tags(conn, "material_tags", "material_id", material_id)

        return _material_to_response(row_to_dict(row), tag_strings)

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
