"""議論トピック管理サービス"""
import re
import sqlite3
from src.db import get_connection, row_to_dict
from src.services.citations_service import upsert_citations_for_owner_with_conn
from src.services.readable_id import strip_entity_id_inplace
from src.services.embedding_service import (
    build_embedding_text,
    generate_and_store_embedding,
    insert_topic_embedding_with_conn,
)
from src.services.relation_service import _add_relation_with_conn, _validate_targets
from src.services.relay.entity_publish import publish_entity_event_with_conn
from src.services.search_service import find_similar_topics
from src.services.title_validation import validate_title
from src.services.tag_service import (
    validate_and_parse_tags,
    ensure_tag_ids,
    resolve_tag_ids,
    link_tags,
    get_entity_tags,
    get_entity_tags_batch,
)

TOPIC_DESC_MAX_LEN = 200


def get_activity_topics_batch(
    conn: sqlite3.Connection, activity_ids: list[int]
) -> dict[int, list[dict]]:
    """アクティビティID群に対し、関連する全topicの (id, title) を一括取得する。

    relations_view を1クエリで叩く
    （D#2465: スキル層から get_map をN回叩かずバックエンドでバッチ取得）。
    relations_view は逆方向展開を含むが、`source_type='activity' AND target_type='topic'`
    でフィルタすることで activity 側を source とする行のみに絞られる。
    relations テーブルの PRIMARY KEY (source_type, source_id, target_type, target_id) が
    重複を保証するため、結果セットに (activity_id, topic_id) の重複は現れない。

    Returns:
        {activity_id: [{"id": int, "title": str}, ...]}
        各 activity_id について topic_id 昇順でソート済み（決定的）。
        関連 topic を持たない activity_id はキー自体が存在しない。
    """
    if not activity_ids:
        return {}
    placeholders = ",".join("?" * len(activity_ids))
    rows = conn.execute(
        f"""SELECT rv.source_id AS activity_id,
                   dt.id AS topic_id,
                   dt.title AS topic_title
            FROM relations_view rv
            JOIN discussion_topics dt ON dt.id = rv.target_id
            WHERE rv.source_type = 'activity'
              AND rv.source_id IN ({placeholders})
              AND rv.target_type = 'topic'
            ORDER BY rv.source_id, dt.id""",
        tuple(activity_ids),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        result.setdefault(r["activity_id"], []).append(
            {"id": r["topic_id"], "title": r["topic_title"]}
        )
    return result


def count_decisions_per_topic(conn: sqlite3.Connection, topic_ids: list[int]) -> dict[int, int]:
    """トピックごとのdecisions件数を取得する（retracted除外）。

    Returns:
        {topic_id: count, ...} — decisionsが0件のtopic_idはキーに含まれない
    """
    if not topic_ids:
        return {}
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"""
        SELECT r.target_id AS topic_id, COUNT(*) AS cnt
        FROM decisions d
        JOIN relations r
          ON r.source_type = 'decision' AND r.source_id = d.id
         AND r.target_type = 'topic'
         AND r.relation_type = 'belongs_to'
         AND r.target_id IN ({placeholders})
        WHERE d.retracted_at IS NULL
        GROUP BY r.target_id
        """,
        tuple(topic_ids),
    ).fetchall()
    return {row["topic_id"]: row["cnt"] for row in rows}


def count_materials_per_topic(conn: sqlite3.Connection, topic_ids: list[int]) -> dict[int, int]:
    """トピックごとに直接紐づくmaterials件数を取得する。

    material→topic の親帰属は relations.relation_type='belongs_to' で表現される
    (正規化制約により source=material, target=topic で格納)。
    partial index `idx_relations_belongs_to_tgt` がこの WHERE 条件でヒットする。

    activity経由の間接リレーションは含めない (それは別経路で集約)。

    Returns:
        {topic_id: count, ...} — materialsが0件のtopic_idはキーに含まれない
    """
    if not topic_ids:
        return {}
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"""
        SELECT target_id AS topic_id, COUNT(*) AS cnt
        FROM relations
        WHERE source_type = 'material'
          AND target_type = 'topic'
          AND relation_type = 'belongs_to'
          AND target_id IN ({placeholders})
        GROUP BY target_id
        """,
        tuple(topic_ids),
    ).fetchall()
    return {row["topic_id"]: row["cnt"] for row in rows}


def get_recent_topics_with_conn(conn, limit: int = 10) -> list[dict]:
    """最近作成されたトピックのID・タイトルを取得する（conn共有版）。

    Returns:
        [{"id": int, "title": str}, ...]（created_at降順）
    """
    rows = conn.execute(
        "SELECT id, title FROM discussion_topics ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def add_topic(
    title: str,
    description: str,
    tags: list[str],
    related: list[dict] | None = None,
) -> dict:
    """
    新しい議論トピックを追加する。

    Args:
        title: トピックのタイトル（35字以内）
        description: トピックの説明（必須）
        tags: タグ配列（必須、1個以上）
        related: 関連エンティティ（optional）。
            [{"type": "topic" | "activity" | "material" | "decision" | "log", "ids": [int, ...]}, ...] 形式。
            複数エンティティを配列で同時紐付け可能。
            例: [{"type": "topic", "ids": [1, 2]}, {"type": "decision", "ids": [10]}]

    Returns:
        作成されたトピック情報
    """
    # titleのバリデーション
    title_err = validate_title(title)
    if title_err:
        return title_err
    # タグのバリデーション
    parsed_tags = validate_and_parse_tags(tags, required=True)
    if isinstance(parsed_tags, dict):
        return parsed_tags

    # relatedのバリデーション
    if related:
        err = _validate_targets("topic", related)
        if err:
            return err

    conn = get_connection()
    try:
        # トピックをINSERT
        cursor = conn.execute(
            "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
            (title, description),
        )
        topic_id = cursor.lastrowid

        # タグをリンク
        tag_ids = ensure_tag_ids(conn, parsed_tags)
        link_tags(conn, "topic_tags", "topic_id", topic_id, tag_ids)

        # リレーションを追加
        if related:
            _add_relation_with_conn(conn, "topic", topic_id, related)

        # 本文中の {{cite:X#NNN}} を citations テーブルに保存
        upsert_citations_for_owner_with_conn(
            conn, "topic", topic_id, title=title, description=description
        )

        publish_entity_event_with_conn(
            conn, entity_type="topic", entity_id=topic_id, event="created"
        )

        conn.commit()

        # タグを取得
        tag_strings = get_entity_tags(conn, "topic_tags", "topic_id", topic_id)

        # embedding生成（失敗してもtopic作成には影響しない）
        tag_text = " ".join(tag_strings) if tag_strings else ""
        embedding_text = build_embedding_text(title, description, tag_text)
        embedding_vec = generate_and_store_embedding("topic", topic_id, embedding_text)
        # topic routing 専用索引にも同じベクトルを書き込む（再エンコードしない）。
        # 既に開いている conn を再利用し、新規コネクション+拡張再ロードを避ける。
        if embedding_vec is not None:
            insert_topic_embedding_with_conn(conn, topic_id, embedding_vec)
            conn.commit()

        # 類似トピックをサジェスト（生成済みembeddingを再利用しHTTPリクエストを削減）
        similar = find_similar_topics(embedding_text, exclude_id=topic_id, embedding=embedding_vec)

        result = {"topic_id": topic_id}
        if similar:
            result["similar_topics"] = similar
        return result

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


def get_topics(
    tags: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """
    トピックを新しい順に取得する（ページネーション付き）。

    Args:
        tags: タグ配列（optional。指定時はAND条件でフィルタ、未指定時は全件）
        limit: 取得件数（デフォルト10）
        offset: スキップ件数（デフォルト0）
        since: ISO日付文字列（例: "2026-03-10"）。この日付以降に作成されたトピックのみ返す
        until: ISO日付文字列。この日付以前に作成されたトピックのみ返す

    Returns:
        トピック一覧（total_count付き）
    """
    # タグのバリデーション（tags指定時のみ）
    parsed_tags = None
    if tags is not None:
        parsed_tags = validate_and_parse_tags(tags, required=True)
        if isinstance(parsed_tags, dict):
            return parsed_tags

    try:
        if limit < 1:
            return {
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": "limit must be >= 1",
                }
            }
        if offset < 0:
            return {
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": "offset must be >= 0",
                }
            }

        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
        if since is not None and not date_pattern.match(since):
            return {
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": f"since must be ISO date format (YYYY-MM-DD), got '{since}'",
                }
            }
        if until is not None and not date_pattern.match(until):
            return {
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": f"until must be ISO date format (YYYY-MM-DD), got '{until}'",
                }
            }

        conn = get_connection()
        try:
            # タグフィルタでtopic_idsを絞り込む（tags指定時のみ）
            topic_ids = None
            if parsed_tags is not None:
                tag_ids = resolve_tag_ids(conn, parsed_tags)
                if not tag_ids or len(tag_ids) < len(parsed_tags):
                    return {"topics": [], "total_count": 0}
                placeholders = ",".join("?" * len(tag_ids))

                topic_ids_rows = conn.execute(
                    f"""
                    SELECT topic_id FROM topic_tags
                    WHERE tag_id IN ({placeholders})
                    GROUP BY topic_id
                    HAVING COUNT(DISTINCT tag_id) = ?
                    """,
                    (*tag_ids, len(tag_ids)),
                ).fetchall()

                topic_ids = [row["topic_id"] for row in topic_ids_rows]

                if not topic_ids:
                    return {"topics": [], "total_count": 0}

            # クエリ組み立て
            conditions = []
            where_params = []

            if topic_ids is not None:
                id_placeholders = ",".join("?" * len(topic_ids))
                conditions.append(f"id IN ({id_placeholders})")
                where_params.extend(topic_ids)

            if since is not None:
                conditions.append("created_at >= ?")
                where_params.append(since)

            if until is not None:
                # 日付のみ指定時は当日を含めるため末尾に時刻を付与
                until_value = until if " " in until else until + " 23:59:59"
                conditions.append("created_at <= ?")
                where_params.append(until_value)

            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)
            else:
                where_clause = ""

            count_row = conn.execute(
                f"SELECT COUNT(*) as count FROM discussion_topics {where_clause}",
                where_params,
            ).fetchone()
            total_count = count_row["count"]

            rows = conn.execute(
                f"""
                SELECT * FROM discussion_topics
                {where_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*where_params, limit, offset),
            ).fetchall()

            # バッチでタグ取得
            fetched_ids = [row["id"] for row in rows]
            tags_map = get_entity_tags_batch(conn, "topic_tags", "topic_id", fetched_ids)

            topics = []
            for row in rows:
                topic = row_to_dict(row)
                item = {
                    "id": topic["id"],
                    "title": topic["title"],
                    "description": (topic["description"] or "")[:TOPIC_DESC_MAX_LEN],
                    "tags": tags_map.get(topic["id"], []),
                    "created_at": topic["created_at"],
                }
                strip_entity_id_inplace(item)
                topics.append(item)

            return {"topics": topics, "total_count": total_count}

        finally:
            conn.close()

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
