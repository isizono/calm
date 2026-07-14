"""資材管理サービス"""
import logging
import os
import re
import sqlite3
from typing import Literal, Optional

import yaml

from src.db import get_connection, row_to_dict
from src.services.readable_id import strip_entity_id_inplace
from src.services.embedding_service import build_embedding_text, generate_and_store_embedding
from src.services.citations_service import (
    apply_and_writeback_conversions,
    apply_raw_to_cite_conversion,
    upsert_citations_for_owner_with_conn,
)
from src.services.relation_service import _add_relation_with_conn, _validate_targets
from src.services.relay.entity_publish import publish_entity_event_with_conn
from src.services.title_validation import validate_title
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
    result = {
        "material_id": material["id"],
        "title": material["title"],
        "content": material["content"],
        "source": material["source"],
        "tags": tags,
        "created_at": material["created_at"],
        "hint": "contentの先頭1-2文は内容の説明・要約にしてください（check-in時にsnippetとして表示されます）",
    }
    if material.get("retracted_at"):
        result["retracted_at"] = material["retracted_at"]
    strip_entity_id_inplace(result, id_key="material_id")
    return result


def add_material(title: str, content: str, tags: list[str], source: str, related: list[dict] | None = None) -> dict:
    """
    資材を追加する

    Args:
        title: 資材のタイトル（35字以内）
        content: 資材の本文
        tags: タグ配列（必須、1個以上）
        source: データの出自
        related: 関連エンティティ（optional）。
            [{"type": "topic" | "activity" | "material" | "decision" | "log", "ids": [int, ...]}, ...] 形式。
            複数エンティティを配列で同時紐付け可能。
            例: [{"type": "activity", "ids": [123]}, {"type": "decision", "ids": [10]}]

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

    title_err = validate_title(title)
    if title_err:
        return title_err

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

        # 生 ID リテラルを {{cite:...}} に変換し、書き換わった本文を DB に書き戻す
        converted = apply_and_writeback_conversions(
            conn,
            entity_type="material",
            entity_id=material_id,
            fields_payload={"title": title, "content": content},
            tool_name="add_material",
            table="materials",
        )
        title = converted["title"]
        content = converted["content"]

        # 本文中の {{cite:X#NNN}} を citations テーブルに保存
        upsert_citations_for_owner_with_conn(
            conn, "material", material_id, title=title, content=content
        )

        # タグを取得（commit前）
        tag_strings = get_entity_tags(conn, "material_tags", "material_id", material_id)

        publish_entity_event_with_conn(
            conn, entity_type="material", entity_id=material_id, event="created"
        )

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
           WHERE m.retracted_at IS NULL
           ORDER BY m.created_at ASC""",
        (activity_id,),
    ).fetchall()
    material_ids = [row["id"] for row in rows]
    tags_map = get_entity_tags_batch(conn, "material_tags", "material_id", material_ids) if material_ids else {}
    result = []
    for row in rows:
        item = {
            "id": row["id"],
            "title": row["title"],
            "snippet": (row["content"] or "")[:SNIPPET_MAX_LEN],
            "source": row["source"],
            "tags": tags_map.get(row["id"], []),
            "created_at": row["created_at"],
        }
        strip_entity_id_inplace(item)
        result.append(item)
    return result


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
        title: New title (optional, 35 chars or less)
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

    title_err = validate_title(title)
    if title_err:
        return title_err

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

        # 生 ID リテラルを {{cite:...}} に変換する。update系はUPDATE対象の
        # material_idが呼び出し時点で既に存在する行のため（add系と異なりINSERT
        # による確定を待つ必要がない）、SET句組み立て前に変換を先に行い、
        # 変換前後の値のどちらを書くかで2段UPDATEになるのを避け1回のUPDATEに統合する。
        # 変換対象は呼び出し引数として明示された field のみ (未指定 field の
        # 既存値は触らない)。content は mode 結合後の effective_content を対象にする。
        conversion = apply_raw_to_cite_conversion(
            conn,
            entity_type="material",
            entity_id=material_id,
            fields_payload={
                k: v
                for k, v in {
                    "title": title,
                    "content": effective_content if content is not None else None,
                }.items()
                if v is not None
            },
            tool_name="update_material",
        )
        converted_fields = conversion["fields"]
        converted_title = converted_fields.get("title")
        converted_content = converted_fields.get("content")

        # Build dynamic SQL for title/content
        set_parts = []
        values = []

        if title is not None:
            set_parts.append("title = ?")
            values.append(converted_title)

        if content is not None:
            set_parts.append("content = ?")
            values.append(converted_content)

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

        # citations 全削除→再投入 (本文無変更でも実施)
        new_title = converted_title if title is not None else row["title"]
        new_content = converted_content if content is not None else row["content"]
        upsert_citations_for_owner_with_conn(
            conn, "material", material_id, title=new_title, content=new_content
        )

        publish_entity_event_with_conn(
            conn, entity_type="material", entity_id=material_id, event="updated"
        )

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


def get_material(material_id: int, include_retracted: bool = False) -> dict:
    """
    資材を全文取得する

    Args:
        material_id: 資材のID
        include_retracted: Trueのとき取り消し済みの資材も取得できる（デフォルトFalse）

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
        if not include_retracted and row["retracted_at"] is not None:
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


DEFAULT_EXPORT_DIR = "~/cc-memory-export"
SLUG_MAX_LEN = 50


def _slugify_title(title: str) -> str:
    """ファイル名に安全な slug を返す。

    ASCII 英数字とハイフン以外を "-" に置換し、連続 "-" を圧縮、
    先頭末尾 "-" を除去、SLUG_MAX_LEN で切り詰め、空文字なら "untitled"。
    日本語 title は事実上全てハイフン化されるため、"untitled" にフォールバックする。
    """
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", title or "")
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) > SLUG_MAX_LEN:
        slug = slug[:SLUG_MAX_LEN].rstrip("-")
    return slug or "untitled"


def _resolve_dest_path(entity_id: int, title: str, dest_path: Optional[str]) -> str:
    """dest_path を 3 パターンで振り分けて絶対パスを返す。

    - None: DEFAULT_EXPORT_DIR/M-{id}-{slug}.md
    - 既存ディレクトリ: そこに M-{id}-{slug}.md
    - それ以外: ユーザー指定パスを絶対パス化して使用（親ディレクトリの自動作成対象）
    """
    slug = _slugify_title(title)
    filename = f"M-{entity_id}-{slug}.md"
    if dest_path is None:
        return os.path.join(os.path.expanduser(DEFAULT_EXPORT_DIR), filename)
    expanded = os.path.expanduser(dest_path)
    if os.path.isdir(expanded):
        return os.path.abspath(os.path.join(expanded, filename))
    return os.path.abspath(expanded)


def _is_within_export_dir(path: str) -> bool:
    """path が DEFAULT_EXPORT_DIR のサブツリー内かを realpath 基準で判定する。

    許可ルート・対象パス双方を realpath で正規化してから比較するため、
    シンボリックリンク経由で許可ルート外へ抜けるパスも配下外と判定される。
    """
    export_root = os.path.realpath(os.path.expanduser(DEFAULT_EXPORT_DIR))
    resolved = os.path.realpath(path)
    return resolved == export_root or resolved.startswith(export_root + os.sep)


def _get_material_relations_with_conn(conn, entity_id: int) -> list[dict]:
    """material に直接紐づく関連エンティティを related 配列にする。

    relations_view は related の正方向・逆方向を UNION ALL 済みのため、material を
    source とする1クエリで双方向の直接関連が揃う。depends_on / supersedes は
    activity / decision 専用で material には該当しない。
    """
    rows = conn.execute(
        "SELECT target_type, target_id FROM relations_view "
        "WHERE source_type = ? AND source_id = ? "
        "ORDER BY target_type, target_id",
        ("material", entity_id),
    ).fetchall()
    return [{"type": r["target_type"], "id": r["target_id"]} for r in rows]


def _build_frontmatter(
    entity_id: int,
    title: str,
    tags: list[str],
    source: str,
    related: list[dict],
    created_at: str,
    updated_at: str,
) -> str:
    """YAML frontmatter 文字列（`---\\n...\\n---\\n`）を返す。"""
    data = {
        "material_id": entity_id,
        "title": title,
        "tags": list(tags),
        "source": source,
        "related": related,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    body = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return f"---\n{body}---\n"


def export_material_to_file(material_id: int, dest_path: Optional[str] = None) -> dict:
    """資材を md ファイルとして出力する。

    書き込み先は DEFAULT_EXPORT_DIR のサブツリー内に限定する。配下外を指す
    dest_path（シンボリックリンク経由の脱出を含む）は VALIDATION_ERROR で拒否し、
    ディレクトリ作成もファイル書き込みも一切行わない。

    Returns:
        成功時: {"path": str, "overwritten": bool, "material_id": int, "title": str}
        失敗時: {"error": {"code": str, "message": str}}
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ?", (material_id,)
        ).fetchone()
        if not row or row["retracted_at"] is not None:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Material with id {material_id} not found",
                }
            }

        material = row_to_dict(row)
        tags = get_entity_tags(conn, "material_tags", "material_id", material_id)
        related = _get_material_relations_with_conn(conn, material_id)
    except Exception as e:
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()

    title = material["title"]
    content = material["content"] or ""
    frontmatter = _build_frontmatter(
        entity_id=material_id,
        title=title,
        tags=tags,
        source=material["source"],
        related=related,
        created_at=material["created_at"],
        updated_at=material.get("updated_at") or material["created_at"],
    )
    body = f"{frontmatter}\n# {title}\n\n{content}"
    if not body.endswith("\n"):
        body += "\n"

    path = _resolve_dest_path(material_id, title, dest_path)
    if not _is_within_export_dir(path):
        allowed = os.path.expanduser(DEFAULT_EXPORT_DIR)
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    f"dest_path must resolve to a location within {allowed}. "
                    f"resolved path: {path}"
                ),
            }
        }
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return {"error": {"code": "IO_ERROR", "message": str(e)}}

    overwritten = os.path.exists(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        return {"error": {"code": "IO_ERROR", "message": str(e)}}

    return {
        "path": path,
        "overwritten": overwritten,
        "material_id": material_id,
        "title": title,
    }
