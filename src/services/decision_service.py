"""決定事項管理サービス"""
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
    parse_tag,
    resolve_tag_ids,
    _append_tag_notes_with_conn,
)
from src.services.direction_service import (
    DIRECTION_NAMESPACE,
    DIRECTION_NAME,
    get_direction_decisions,
)
from src.services.habit_service import _add_habit_with_conn
from src.services.precedent_pure import parse_precedent_sections, summarize_precedent
from src.services.relation_service import _add_relation_with_conn
from src.services.supersede_service import compute_supersede_info_batch
from src.services.title_validation import validate_title

PROPAGATE_TYPES = {"habit", "tag_note"}


def add_decisions(items: list[dict], caller_session_id: Optional[str] = None) -> dict:
    """
    複数の決定事項を一括記録する（最大10件）。

    SAVEPOINT方式で各アイテムを個別に処理し、部分成功を許容する。
    embedding生成はcreated分のみ一括で行う。

    Args:
        items: 決定事項情報のリスト。各要素は以下のキーを持つ:
            - topic_id (int, 必須): 関連するトピックのID
            - decision (str, 必須): 決定内容
            - reason (str, 必須): 決定の理由
            - title (str, optional): 決定の要点を表す1行（40字以内）。省略時はNULL（表示はdecision本文にfallback）。
              tagsに layer:direction を含む場合は必須（省略・空文字はエラー）
            - tags (list[str], optional): 追加タグ。省略時はtopicのタグを継承。layer:direction は
              人間の抽象方向性判断であることを明示するタグ（付けた場合はtitle必須）

    Returns:
        {created: [...], errors: [{index, error}]}
        created各要素には related_decisions（同topic内の類似decision上位3件 [{id, title, distance}]）が付く。
        tagsに layer:direction を含む要素には existing_direction_decisions（同domainの有効な
        方向性decision全件、自身除外・非ランク）と direction_note（supersede/併存の判断を促す文言）も付く。
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
                decision = item.get("decision", "")
                reason = item.get("reason", "")
                # 空文字・空白のみのtitleはNULLへ正規化する。表示fallbackは
                # SQL側がCOALESCE(NULLのみfallback)・Python側が`or`(""もfallback)で
                # 意味論が分かれるため、""をNULLに寄せて全箇所の挙動を一致させる。
                title = (item.get("title") or "").strip() or None
                tags = item.get("tags")

                # titleのバリデーション（None は skip）
                title_err = validate_title(title)
                if title_err:
                    raise ValueError(title_err["error"]["message"])

                # タグのバリデーション（tagsが指定された場合のみ）
                parsed_tags = None
                if tags is not None:
                    parsed_tags = validate_and_parse_tags(tags)
                    if isinstance(parsed_tags, dict):
                        raise ValueError(parsed_tags["error"]["message"])

                # layer:direction タグ付きitemはtitle必須（少数・明示の原則。
                # 一覧で一目で識別できる必要があるため通常decisionより摩擦を高くする）
                is_direction_item = bool(
                    parsed_tags and (DIRECTION_NAMESPACE, DIRECTION_NAME) in parsed_tags
                )
                if is_direction_item and title is None:
                    raise ValueError(
                        f"title is required for {DIRECTION_NAMESPACE}:{DIRECTION_NAME} decisions"
                    )

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

                # decisionをINSERT (親 topic は relations.belongs_to で表現するため topic_id は持たせない)
                cursor = conn.execute(
                    "INSERT INTO decisions (decision, reason, title, caller_session_id) VALUES (?, ?, ?, ?)",
                    (decision, reason, title, caller_session_id),
                )
                decision_id = cursor.lastrowid

                # 親 topic との belongs_to リレーションを記録
                if topic_id is not None:
                    _add_relation_with_conn(
                        conn, "decision", decision_id,
                        [{"type": "topic", "ids": [topic_id]}],
                    )

                # タグをリンク（指定された場合のみ）
                if parsed_tags:
                    tag_ids = ensure_tag_ids(conn, parsed_tags)
                    link_tags(conn, "decision_tags", "decision_id", decision_id, tag_ids)

                # 本文中の {{cite:X#NNN}} を citations テーブルに保存
                upsert_citations_for_owner_with_conn(
                    conn, "decision", decision_id, decision=decision, reason=reason
                )

                # propagate_to 処理
                propagate_to = item.get("propagate_to")
                propagation_result = None
                if propagate_to:
                    conn.execute(f"SAVEPOINT propagate_{i}")
                    try:
                        p_type = propagate_to.get("type")
                        p_content = propagate_to.get("content", "")
                        if p_type not in PROPAGATE_TYPES:
                            raise ValueError(f"Invalid propagate_to.type: {p_type}")
                        if p_type == "habit":
                            p_id = _add_habit_with_conn(conn, p_content)
                            propagation_result = {"status": "ok", "type": "habit", "id": p_id}
                        elif p_type == "tag_note":
                            p_tag = propagate_to.get("tag")
                            if not p_tag:
                                raise ValueError("propagate_to.tag is required when type is 'tag_note'")
                            p_id = _append_tag_notes_with_conn(conn, p_tag, p_content)
                            propagation_result = {"status": "ok", "type": "tag_note", "id": p_id}
                        conn.execute(f"RELEASE SAVEPOINT propagate_{i}")
                    except Exception as e:
                        conn.execute(f"ROLLBACK TO SAVEPOINT propagate_{i}")
                        conn.execute(f"RELEASE SAVEPOINT propagate_{i}")
                        propagation_result = {"status": "error", "type": propagate_to.get("type", "unknown"), "message": str(e)}

                conn.execute(f"RELEASE SAVEPOINT item_{i}")
                created_item = {
                    "decision_id": decision_id,
                    "topic_id": topic_id,
                    "decision": decision,
                    "reason": reason,
                    "_is_direction_item": is_direction_item,
                }
                if propagation_result:
                    created_item["propagation"] = propagation_result
                created.append(created_item)

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
            created_ids = [c["decision_id"] for c in created]
            tags_map = get_effective_tags_batch_by_ids(conn, "decision", created_ids)

            # created_atを一括取得
            placeholders = ",".join("?" * len(created_ids))
            rows = conn.execute(
                f"SELECT id, created_at FROM decisions WHERE id IN ({placeholders})",
                tuple(created_ids),
            ).fetchall()
            created_at_map = {row["id"]: row["created_at"] for row in rows}

            for c in created:
                c["tags"] = tags_map.get(c["decision_id"], [])
                c["created_at"] = created_at_map.get(c["decision_id"])

            # embedding一括生成 + 同topic内の関連decision取得（created分のみ。失敗してもエラーにしない）
            # 関連decisionは矛盾・重複への気づきを促す導線。embeddingサーバー未起動時は空リスト。
            # search_serviceは関数内importでcircular import（decision→search→...）を回避する。
            from src.services import search_service
            for c in created:
                tag_text = " ".join(c["tags"]) if c["tags"] else ""
                embedding = generate_and_store_embedding(
                    "decision", c["decision_id"],
                    build_embedding_text(c["decision"], c["reason"], tag_text),
                )
                related = []
                if embedding is not None and c["topic_id"] is not None:
                    related = search_service.find_similar_decisions(
                        exclude_id=c["decision_id"],
                        topic_id=c["topic_id"],
                        embedding=embedding,
                    )
                c["related_decisions"] = related

                # layer:direction item には同domainの既存active方向性decisionを
                # 網羅列挙して付ける（矛盾・重複気づき導線のrelated_decisionsと違い
                # ランク検索に依存しない全件。少数性の前提でrecallを壊さない）
                if c.pop("_is_direction_item", False):
                    domain_tag_ids = resolve_tag_ids(
                        conn,
                        [parse_tag(t) for t in c["tags"] if t.startswith("domain:")],
                    )
                    existing = [
                        d for d in get_direction_decisions(conn, domain_tag_ids=domain_tag_ids)
                        if d["id"] != c["decision_id"]
                    ]
                    c["existing_direction_decisions"] = [
                        {"id": d["id"], "title": d["title"], "created_at": d["created_at"]}
                        for d in existing
                    ]
                    # domainタグが解決できないと get_direction_decisions は全domain横断で
                    # 返すため、文言も「同domain」ではなく横断であることを正確に示す
                    scope_label = (
                        "同domainに" if domain_tag_ids
                        else "全domain横断で（このdecisionにdomainタグが無いため）"
                    )
                    c["direction_note"] = (
                        f"{scope_label}有効な方向性decisionが{len(existing)}件あります。"
                        "この決定が置き換えるものにはadd_relation(relation_type='supersedes')"
                        "を張ってください。併存する場合は、併存理由をreasonに明記してください。"
                    )

            # レスポンス軽量化: embedding生成後はdecision_id/related_decisions(+direction系)以外を除去
            for c in created:
                c.pop("decision", None)
                c.pop("reason", None)
                c.pop("topic_id", None)
                c.pop("tags", None)
                c.pop("created_at", None)

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


def _build_decision_item(
    dec: dict,
    tags_map: dict[int, list[str]],
    supersede_map: dict[int, dict],
) -> dict:
    """SELECT * FROM decisions の 1 行から返却用の decision item を組み立てる。

    is_superseded / is_retracted / supersede_chain をここで付与する。詳細:
    - is_retracted は decisions.retracted_at の NOT NULL 判定
    - is_superseded / supersede_chain は supersede_service.compute_supersede_info_batch の結果
    - retracted_at 生値は従来通り retracted 済みのときのみ含める (retract 時刻が呼出側で必要)

    reason に `docs/precedent-format.md` の定型節（却下案:/適用条件:/適用外:/検証:）が
    あれば precedent（コンパクト形）を付与する。節が無い decision にはキーを付けない
    （legacy 本文と規約準拠本文を区別できるようにする）。
    """
    display_title = dec.get("title") or (dec["decision"] or "")[:50]
    supersede_info = supersede_map.get(
        dec["id"], {"is_superseded": False, "supersede_chain": [dec["id"]]}
    )
    item = {
        "id": dec["id"],
        "title": display_title,
        "decision": dec["decision"],
        "reason": dec["reason"],
        "tags": tags_map.get(dec["id"], []),
        "created_at": dec["created_at"],
        "is_superseded": supersede_info["is_superseded"],
        "is_retracted": bool(dec.get("retracted_at")),
        "supersede_chain": supersede_info["supersede_chain"],
    }
    if dec.get("retracted_at"):
        item["retracted_at"] = dec["retracted_at"]
    parsed_precedent = parse_precedent_sections(dec.get("reason") or "")
    if parsed_precedent is not None:
        item["precedent"] = summarize_precedent(parsed_precedent)
    apply_readable_id_inplace(item, "decision")
    return item


def get_decisions(
    entity_type: str,
    entity_id: int,
    start_id: Optional[int] = None,
    limit: int = 30,
    include_retracted: bool = False,
) -> dict:
    """
    指定エンティティに関連する決定事項を取得する。

    Args:
        entity_type: エンティティタイプ（"topic" または "activity"）
        entity_id: 対象エンティティのID
        start_id: 取得開始位置の決定事項ID（ページネーション用）
        limit: 取得件数上限（最大30件）

    Returns:
        決定事項一覧（各decisionにtags付き）
        entity_type == "topic": 従来通りtopic_idで直接取得
        entity_type == "activity": related topics（上限10件）経由でdecisions集約
    """
    retract_filter = "" if include_retracted else " AND retracted_at IS NULL"

    conn = get_connection()
    try:
        # limitを30件に制限
        limit = min(limit, 30)

        if entity_type == "topic":
            topic_id = entity_id

            # topic_nameを取得
            topic_row = conn.execute(
                "SELECT title FROM discussion_topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
            topic_name = topic_row["title"] if topic_row else None

            if topic_name is None:
                return {
                    "topic_id": topic_id,
                    "topic_name": None,
                    "decisions": [],
                }

            # decisions の親 topic は relations.belongs_to 経由で解決する
            decision_retract_filter = retract_filter.replace("retracted_at", "d.retracted_at")
            if start_id is None:
                rows = conn.execute(
                    f"""
                    SELECT d.* FROM decisions d
                    JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                                    AND r.target_type = 'topic' AND r.target_id = ?
                                    AND r.relation_type = 'belongs_to'
                    WHERE 1=1{decision_retract_filter}
                    ORDER BY d.created_at ASC, d.id ASC
                    LIMIT ?
                    """,
                    (topic_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT d.* FROM decisions d
                    JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                                    AND r.target_type = 'topic' AND r.target_id = ?
                                    AND r.relation_type = 'belongs_to'
                    WHERE d.id >= ?{decision_retract_filter}
                    ORDER BY d.created_at ASC, d.id ASC
                    LIMIT ?
                    """,
                    (topic_id, start_id, limit),
                ).fetchall()

            # バッチでタグ取得
            tags_map = get_effective_tags_batch(conn, "decision", topic_id)
            decision_ids = [row_to_dict(row)["id"] for row in rows]
            supersede_map = compute_supersede_info_batch(conn, decision_ids)

            decisions = []
            for row in rows:
                dec = row_to_dict(row)
                item = _build_decision_item(dec, tags_map, supersede_map)
                decisions.append(item)

            return {
                "topic_id": topic_id,
                "topic_name": topic_name,
                "decisions": decisions,
            }

        elif entity_type == "activity":
            # activity → related topics（上限10件）→ decisions集約
            relation_rows = conn.execute(
                "SELECT target_type, target_id FROM relations_view WHERE source_type = ? AND source_id = ?",
                ("activity", entity_id),
            ).fetchall()
            topic_ids = [r["target_id"] for r in relation_rows if r["target_type"] == "topic"][:10]

            if not topic_ids:
                return {"decisions": []}

            placeholders = ",".join("?" * len(topic_ids))
            # decisions の親 topic 集約も relations.belongs_to 経由。
            # DISTINCT で複数 topic に belongs_to する decision の重複を抑制
            decision_retract_filter = retract_filter.replace("retracted_at", "d.retracted_at")
            if start_id is None:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT d.* FROM decisions d
                    JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                                    AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                                    AND r.target_id IN ({placeholders})
                    WHERE 1=1{decision_retract_filter}
                    ORDER BY d.id DESC
                    LIMIT ?
                    """,
                    tuple(topic_ids) + (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT d.* FROM decisions d
                    JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                                    AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                                    AND r.target_id IN ({placeholders})
                    WHERE d.id <= ?{decision_retract_filter}
                    ORDER BY d.id DESC
                    LIMIT ?
                    """,
                    tuple(topic_ids) + (start_id, limit),
                ).fetchall()

            # 全topic_idを横断してバッチでタグ取得
            decision_ids = [row_to_dict(row)["id"] for row in rows]
            tags_map = get_effective_tags_batch_by_ids(conn, "decision", decision_ids) if decision_ids else {}
            supersede_map = compute_supersede_info_batch(conn, decision_ids)

            decisions = []
            for row in rows:
                dec = row_to_dict(row)
                item = _build_decision_item(dec, tags_map, supersede_map)
                decisions.append(item)

            return {"decisions": decisions}

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
