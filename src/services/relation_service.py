"""エンティティ間リレーション管理サービス"""
import logging
import sqlite3

from src.db import get_connection
from src.services.readable_id import apply_readable_id_inplace
from src.services.tag_service import (
    get_entity_tags_batch,
)

logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES = {"topic", "activity", "material", "decision", "log"}
VALID_RELATION_TYPES = {"related", "depends_on", "supersedes", "belongs_to"}

# 親帰属パターン: 子→親 (decision/log/material/activity → topic) を表す relations 行は
# 自動的に 'belongs_to' で書き込む。正規化制約により親が必ず target 側に来る
_PARENT_CHILD_TYPES = {"activity", "material", "decision", "log"}


def _resolve_relation_type(n_stype: str, n_ttype: str, requested: str) -> str:
    """正規化後のペアと要求された relation_type から、実際に書き込む relation_type を決める。

    親帰属パターン (子 → topic) の 'related' は自動的に 'belongs_to' に格上げする。
    親帰属でないペアに対する 'belongs_to' の指定は呼び出し元側で弾く想定。
    """
    if n_ttype == "topic" and n_stype in _PARENT_CHILD_TYPES:
        return "belongs_to"
    return requested


def _validate_entity_type(entity_type: str) -> dict | None:
    """エンティティタイプをバリデーションする。不正な場合はエラーdictを返す。"""
    if entity_type not in VALID_ENTITY_TYPES:
        return {
            "error": {
                "code": "INVALID_ENTITY_TYPE",
                "message": f"Invalid entity type: '{entity_type}'. Must be one of {sorted(VALID_ENTITY_TYPES)}",
            }
        }
    return None


def _validate_targets(source_type: str, targets: list[dict]) -> dict | None:
    """targetsのバリデーション。不正な場合はエラーdictを返す。"""
    if not targets:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "targets must not be empty",
            }
        }
    for target in targets:
        if "type" not in target or "ids" not in target:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Each target must have 'type' and 'ids' fields",
                }
            }
        err = _validate_entity_type(target["type"])
        if err:
            return err
        if not isinstance(target["ids"], list) or not target["ids"]:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"'ids' for type '{target['type']}' must be a non-empty list",
                }
            }
    return None


def _normalize_pair(source_type: str, source_id: int, target_type: str, target_id: int):
    """source/targetペアを正規化する（source_type < target_type、同一typeならsource_id < target_id）。

    Returns:
        (source_type, source_id, target_type, target_id) or None（自己参照の場合）
    """
    if source_type == target_type and source_id == target_id:
        return None

    if source_type < target_type:
        return (source_type, source_id, target_type, target_id)
    elif source_type > target_type:
        return (target_type, target_id, source_type, source_id)
    else:
        # 同一type: id順で正規化
        if source_id < target_id:
            return (source_type, source_id, target_type, target_id)
        else:
            return (source_type, target_id, target_type, source_id)


def _has_dependency_path(conn: sqlite3.Connection, from_id: int, to_id: int) -> bool:
    """DFSでfrom_idからto_idへのdepends_on経路が存在するか判定する。

    activity_dependenciesテーブルを辿り、from_id → ... → to_id の到達可能性をチェックする。
    循環依存検出に使用: 新たに dependent→dependency を追加する前に、
    dependency→dependent への既存経路があればサイクルになる。
    """
    visited: set[int] = set()
    stack = [from_id]
    while stack:
        current = stack.pop()
        if current == to_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        rows = conn.execute(
            "SELECT dependency_id FROM activity_dependencies WHERE dependent_id = ?",
            (current,),
        ).fetchall()
        for row in rows:
            stack.append(row["dependency_id"])
    return False


def _add_depends_on_with_conn(conn: sqlite3.Connection, source_id: int, target_ids: list[int]) -> int:
    """depends_onリレーションをactivity_dependenciesテーブルに追加する。

    循環依存を検出した場合はValueErrorを送出する。

    Args:
        conn: DB接続
        source_id: 依存元（dependent）のアクティビティID
        target_ids: 依存先（dependency）のアクティビティIDリスト

    Returns:
        追加件数

    Raises:
        ValueError: 循環依存が検出された場合
    """
    added = 0
    for target_id in target_ids:
        # 自己参照はCHECK制約で弾かれるが、明示的にスキップ
        if source_id == target_id:
            continue

        # 循環チェック: target_id → source_id への経路が既に存在すればサイクル
        if _has_dependency_path(conn, target_id, source_id):
            raise ValueError(
                f"Circular dependency detected: adding {source_id}→{target_id} "
                f"would create a cycle"
            )

        conn.execute(
            "INSERT OR IGNORE INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
            (source_id, target_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] > 0:
            added += 1
    return added


def _add_relation_with_conn(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: int,
    targets: list[dict],
    relation_type: str = "related",
) -> int:
    """conn共有版: リレーションを追加する。追加件数を返す。

    親帰属パターン (子 → topic) は自動的に 'belongs_to' に格上げされる。
    それ以外は relation_type 引数の値 (default: 'related') で書き込む。
    """
    added = 0
    for target in targets:
        target_type = target["type"]
        for target_id in target["ids"]:
            normalized = _normalize_pair(source_type, source_id, target_type, target_id)
            if normalized is None:
                # 自己参照はスキップ
                continue
            n_stype, n_sid, n_ttype, n_tid = normalized
            rtype = _resolve_relation_type(n_stype, n_ttype, relation_type)
            conn.execute(
                "INSERT OR IGNORE INTO relations (source_type, source_id, target_type, target_id, relation_type) VALUES (?, ?, ?, ?, ?)",
                (n_stype, n_sid, n_ttype, n_tid, rtype),
            )
            # INSERT OR IGNOREの場合、重複時はchanges()=0
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                added += 1
    return added


def _remove_depends_on_with_conn(conn: sqlite3.Connection, source_id: int, target_ids: list[int]) -> int:
    """depends_onリレーションをactivity_dependenciesテーブルから削除する。

    Args:
        conn: DB接続
        source_id: 依存元（dependent）のアクティビティID
        target_ids: 依存先（dependency）のアクティビティIDリスト

    Returns:
        削除件数
    """
    removed = 0
    for target_id in target_ids:
        # 自己参照はCHECK制約で存在し得ないが、明示的にスキップ
        if source_id == target_id:
            continue
        conn.execute(
            "DELETE FROM activity_dependencies WHERE dependent_id = ? AND dependency_id = ?",
            (source_id, target_id),
        )
        removed += conn.execute("SELECT changes()").fetchone()[0]
    return removed


def _remove_relation_with_conn(conn: sqlite3.Connection, source_type: str, source_id: int, targets: list[dict]) -> int:
    """conn共有版: リレーションを削除する。削除件数を返す。"""
    removed = 0
    for target in targets:
        target_type = target["type"]
        for target_id in target["ids"]:
            normalized = _normalize_pair(source_type, source_id, target_type, target_id)
            if normalized is None:
                continue
            n_stype, n_sid, n_ttype, n_tid = normalized
            conn.execute(
                "DELETE FROM relations WHERE source_type = ? AND source_id = ? AND target_type = ? AND target_id = ?",
                (n_stype, n_sid, n_ttype, n_tid),
            )
            removed += conn.execute("SELECT changes()").fetchone()[0]
    return removed


def _validate_depends_on_constraints(source_type: str, targets: list[dict]) -> dict | None:
    """depends_onリレーションの制約をバリデーションする。activity→activityのみ有効。"""
    if source_type != "activity":
        return {
            "error": {
                "code": "INVALID_RELATION_TYPE",
                "message": "depends_on relation is only valid for activity→activity",
            }
        }
    for target in targets:
        if target["type"] != "activity":
            return {
                "error": {
                    "code": "INVALID_RELATION_TYPE",
                    "message": "depends_on relation is only valid for activity→activity",
                }
            }
    return None


def _validate_supersedes_constraints(source_type: str, targets: list[dict]) -> dict | None:
    """supersedesリレーションの制約をバリデーションする。decision→decisionのみ有効。"""
    if source_type != "decision":
        return {
            "error": {
                "code": "INVALID_RELATION_TYPE",
                "message": "supersedes relation is only valid for decision→decision",
            }
        }
    for target in targets:
        if target["type"] != "decision":
            return {
                "error": {
                    "code": "INVALID_RELATION_TYPE",
                    "message": "supersedes relation is only valid for decision→decision",
                }
            }
    return None


def _has_supersedes_path(conn: sqlite3.Connection, from_id: int, to_id: int) -> bool:
    """DFSでfrom_idからto_idへのsupersedes経路が存在するか判定する。

    decision_supersedesテーブルを辿り、from_id → ... → to_id の到達可能性をチェックする。
    循環検出に使用: 新たに source→target を追加する前に、
    target→source への既存経路があればサイクルになる。
    """
    visited: set[int] = set()
    stack = [from_id]
    while stack:
        current = stack.pop()
        if current == to_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        rows = conn.execute(
            "SELECT target_id FROM decision_supersedes WHERE source_id = ?",
            (current,),
        ).fetchall()
        for row in rows:
            stack.append(row["target_id"])
    return False


def _add_supersedes_with_conn(conn: sqlite3.Connection, source_id: int, target_ids: list[int]) -> tuple[int, int]:
    """supersedesリレーションをdecision_supersedesテーブルに追加する。

    循環を検出した場合はValueErrorを送出する。
    INSERT成功時のみ pins の付け替えを同一トランザクション内で実行する
    （重複add（changes()=0）では発火しない）。

    Args:
        conn: DB接続
        source_id: 上書き元（新）のdecision ID
        target_ids: 上書き先（旧）のdecision IDリスト

    Returns:
        (追加件数, pin付け替え件数)

    Raises:
        ValueError: 循環が検出された場合
    """
    # pin_service との循環import回避のためローカルimport
    from src.services.pin_service import _transfer_pins_with_conn

    added = 0
    pins_transferred = 0
    for target_id in target_ids:
        # 自己参照はCHECK制約で弾かれるが、明示的にスキップ
        if source_id == target_id:
            continue

        # 循環チェック: target_id → source_id への経路が既に存在すればサイクル
        if _has_supersedes_path(conn, target_id, source_id):
            raise ValueError(
                f"Circular supersedes detected: adding {source_id}→{target_id} "
                f"would create a cycle"
            )

        conn.execute(
            "INSERT OR IGNORE INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
            (source_id, target_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] > 0:
            added += 1
            # INSERT成功時のみpin付け替えを実行（superseded側=target_id, superseder側=source_id）
            pins_transferred += _transfer_pins_with_conn(conn, "decision", target_id, source_id)
    return added, pins_transferred


def _remove_supersedes_with_conn(conn: sqlite3.Connection, source_id: int, target_ids: list[int]) -> int:
    """supersedesリレーションをdecision_supersedesテーブルから削除する。

    Args:
        conn: DB接続
        source_id: 上書き元のdecision ID
        target_ids: 上書き先のdecision IDリスト

    Returns:
        削除件数
    """
    removed = 0
    for target_id in target_ids:
        # 自己参照はCHECK制約で存在し得ないが、明示的にスキップ
        if source_id == target_id:
            continue
        conn.execute(
            "DELETE FROM decision_supersedes WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        )
        removed += conn.execute("SELECT changes()").fetchone()[0]
    return removed


def add_relation(source_type: str, source_id: int, targets: list[dict], relation_type: str = "related") -> dict:
    """リレーションを追加する。

    Args:
        source_type: 起点エンティティのタイプ（"topic", "activity", "material", "decision", or "log"）
        source_id: 起点エンティティのID
        targets: ターゲットリスト [{"type": "topic", "ids": [1, 2]}, ...]
        relation_type: リレーションタイプ（"related", "depends_on", or "supersedes"）。
            "depends_on" はactivity同士のみ有効で、循環依存を検出した場合はエラーを返す。
            "supersedes" はdecision同士のみ有効で、循環を検出した場合はエラーを返す。

    Returns:
        成功時: {"added": int}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if relation_type not in VALID_RELATION_TYPES:
        return {
            "error": {
                "code": "INVALID_RELATION_TYPE",
                "message": f"Invalid relation_type: '{relation_type}'. Must be one of {sorted(VALID_RELATION_TYPES)}",
            }
        }

    err = _validate_entity_type(source_type)
    if err:
        return err
    err = _validate_targets(source_type, targets)
    if err:
        return err

    if relation_type == "depends_on":
        err = _validate_depends_on_constraints(source_type, targets)
        if err:
            return err

    if relation_type == "supersedes":
        err = _validate_supersedes_constraints(source_type, targets)
        if err:
            return err

    conn = get_connection()
    try:
        if relation_type == "depends_on":
            added = 0
            for target in targets:
                added += _add_depends_on_with_conn(conn, source_id, target["ids"])
            conn.commit()
            return {"added": added}
        elif relation_type == "supersedes":
            added = 0
            pins_transferred = 0
            for target in targets:
                a, p = _add_supersedes_with_conn(conn, source_id, target["ids"])
                added += a
                pins_transferred += p
            conn.commit()
            result: dict = {"added": added}
            if pins_transferred > 0:
                result["pins_transferred"] = pins_transferred
                logger.info(
                    f"supersedes added: source_id={source_id}, "
                    f"added={added}, pins_transferred={pins_transferred}"
                )
            return result
        else:
            added = _add_relation_with_conn(conn, source_type, source_id, targets, relation_type)
        conn.commit()
        return {"added": added}
    except ValueError as e:
        conn.rollback()
        logger.warning(f"add_relation rejected: {e}")
        code = "CIRCULAR_SUPERSEDES" if relation_type == "supersedes" else "CIRCULAR_DEPENDENCY"
        return {"error": {"code": code, "message": str(e)}}
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"add_relation failed: {e}")
        return {"error": {"code": "CONSTRAINT_VIOLATION", "message": str(e)}}
    except Exception as e:
        conn.rollback()
        logger.error(f"add_relation failed: {e}")
        return {"error": {"code": "ADD_RELATION_FAILED", "message": str(e)}}
    finally:
        conn.close()


def remove_relation(source_type: str, source_id: int, targets: list[dict], relation_type: str = "related") -> dict:
    """リレーションを削除する。

    Args:
        source_type: 起点エンティティのタイプ（"topic", "activity", "material", "decision", or "log"）
        source_id: 起点エンティティのID
        targets: ターゲットリスト [{"type": "topic", "ids": [1, 2]}, ...]
        relation_type: リレーションタイプ（"related", "depends_on", or "supersedes"）。
            "depends_on" はactivity同士のみ有効。
            "supersedes" はdecision同士のみ有効。

    Returns:
        成功時: {"removed": int}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if relation_type not in VALID_RELATION_TYPES:
        return {
            "error": {
                "code": "INVALID_RELATION_TYPE",
                "message": f"Invalid relation_type: '{relation_type}'. Must be one of {sorted(VALID_RELATION_TYPES)}",
            }
        }

    err = _validate_entity_type(source_type)
    if err:
        return err
    err = _validate_targets(source_type, targets)
    if err:
        return err

    if relation_type == "depends_on":
        err = _validate_depends_on_constraints(source_type, targets)
        if err:
            return err

    if relation_type == "supersedes":
        err = _validate_supersedes_constraints(source_type, targets)
        if err:
            return err

    conn = get_connection()
    try:
        if relation_type == "depends_on":
            removed = 0
            for target in targets:
                removed += _remove_depends_on_with_conn(conn, source_id, target["ids"])
        elif relation_type == "supersedes":
            removed = 0
            for target in targets:
                removed += _remove_supersedes_with_conn(conn, source_id, target["ids"])
        else:
            removed = _remove_relation_with_conn(conn, source_type, source_id, targets)
        conn.commit()
        return {"removed": removed}
    except Exception as e:
        conn.rollback()
        logger.error(f"remove_relation failed: {e}")
        return {"error": {"code": "REMOVE_RELATION_FAILED", "message": str(e)}}
    finally:
        conn.close()


def _get_map_with_conn(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    min_depth: int = 0,
    max_depth: int = 2,
) -> list[dict]:
    """conn共有版: 再帰CTEでリレーショングラフを走査し、到達可能エンティティを返す。"""
    rows = conn.execute(
        """
        WITH RECURSIVE reachable(entity_type, entity_id, depth) AS (
            SELECT ?, ?, 0
            UNION
            SELECT r.target_type, r.target_id, re.depth + 1
            FROM reachable re
            JOIN relations_view r
              ON r.source_type = re.entity_type AND r.source_id = re.entity_id
            WHERE re.depth < ?
        )
        SELECT DISTINCT entity_type, entity_id, MIN(depth) AS depth
        FROM reachable
        WHERE depth >= ? AND entity_type IN ('topic', 'activity', 'material')
        GROUP BY entity_type, entity_id
        """,
        (entity_type, entity_id, max_depth, min_depth),
    ).fetchall()

    # エンティティのタイプ別にIDを収集
    topic_ids = [row["entity_id"] for row in rows if row["entity_type"] == "topic"]
    activity_ids = [row["entity_id"] for row in rows if row["entity_type"] == "activity"]
    material_ids = [row["entity_id"] for row in rows if row["entity_type"] == "material"]

    # タイトルをバッチ取得
    topic_titles = {}
    if topic_ids:
        placeholders = ",".join("?" * len(topic_ids))
        title_rows = conn.execute(
            f"SELECT id, title FROM discussion_topics WHERE id IN ({placeholders})",
            tuple(topic_ids),
        ).fetchall()
        topic_titles = {r["id"]: r["title"] for r in title_rows}

    activity_titles = {}
    if activity_ids:
        placeholders = ",".join("?" * len(activity_ids))
        title_rows = conn.execute(
            f"SELECT id, title FROM activities WHERE id IN ({placeholders})",
            tuple(activity_ids),
        ).fetchall()
        activity_titles = {r["id"]: r["title"] for r in title_rows}

    material_titles = {}
    if material_ids:
        placeholders = ",".join("?" * len(material_ids))
        title_rows = conn.execute(
            f"SELECT id, title FROM materials WHERE id IN ({placeholders})",
            tuple(material_ids),
        ).fetchall()
        material_titles = {r["id"]: r["title"] for r in title_rows}

    # タグをバッチ取得
    topic_tags_map = get_entity_tags_batch(conn, "topic_tags", "topic_id", topic_ids) if topic_ids else {}
    activity_tags_map = get_entity_tags_batch(conn, "activity_tags", "activity_id", activity_ids) if activity_ids else {}
    material_tags_map = get_entity_tags_batch(conn, "material_tags", "material_id", material_ids) if material_ids else {}

    # topicエンティティの重力カウント（decisions/materials）をバッチ取得
    # topic_service → relation_service の循環import回避のためローカルimport
    from src.services.topic_service import (
        count_decisions_per_topic,
        count_materials_per_topic,
    )
    topic_decisions_counts = count_decisions_per_topic(conn, topic_ids) if topic_ids else {}
    topic_materials_counts = count_materials_per_topic(conn, topic_ids) if topic_ids else {}

    # 存在するIDのセットを構築（存在しないIDを除外するため）
    existing_ids = set()
    existing_ids.update(("topic", tid) for tid in topic_titles)
    existing_ids.update(("activity", aid) for aid in activity_titles)
    existing_ids.update(("material", mid) for mid in material_titles)

    # カタログ構築（存在しないエンティティは除外）
    entities = []
    for row in rows:
        etype = row["entity_type"]
        eid = row["entity_id"]
        depth = row["depth"]

        if (etype, eid) not in existing_ids:
            continue

        if etype == "topic":
            title = topic_titles[eid]
            tags = topic_tags_map.get(eid, [])
        elif etype == "activity":
            title = activity_titles[eid]
            tags = activity_tags_map.get(eid, [])
        elif etype == "material":
            title = material_titles[eid]
            tags = material_tags_map.get(eid, [])
        else:
            continue

        entity = {
            "type": etype,
            "id": eid,
            "title": title,
            "tags": tags,
            "depth": depth,
        }

        # topicのみ重力カウントを付与（activity/materialには付与しない）
        if etype == "topic":
            entity["decisions_count"] = topic_decisions_counts.get(eid, 0)
            entity["materials_count"] = topic_materials_counts.get(eid, 0)

        # α化（type は etype 確定済み）
        apply_readable_id_inplace(entity, etype)
        entities.append(entity)

    # depth順、同depth内はtype→id順でソート（id_raw は α化後も整数で残るのでそれを使う）
    entities.sort(key=lambda e: (e["depth"], e["type"], e["id_raw"]))

    return entities


def get_map(entity_type: str, entity_id: int, min_depth: int = 0, max_depth: int = 2) -> dict:
    """リレーショングラフを走査し、到達可能エンティティのカタログを返す。

    decision/logノードはグラフ走査の経由ノードとして使用するが、
    返却するカタログにはtopic/activity/materialのみ含める。

    Args:
        entity_type: 起点エンティティのタイプ（"topic", "activity", "material", "decision", or "log"）
        entity_id: 起点エンティティのID
        min_depth: 最小深度（デフォルト: 0）
        max_depth: 最大深度（デフォルト: 2）

    Returns:
        成功時: {"entities": [...], "total_count": int}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    err = _validate_entity_type(entity_type)
    if err:
        return err

    if min_depth < 0:
        return {
            "error": {
                "code": "INVALID_PARAMETER",
                "message": "min_depth must be >= 0",
            }
        }
    if max_depth < min_depth:
        return {
            "error": {
                "code": "INVALID_PARAMETER",
                "message": "max_depth must be >= min_depth",
            }
        }
    if max_depth > 10:
        return {
            "error": {
                "code": "INVALID_PARAMETER",
                "message": "max_depth must be <= 10",
            }
        }

    conn = get_connection()
    try:
        entities = _get_map_with_conn(conn, entity_type, entity_id, min_depth, max_depth)
        return {
            "entities": entities,
            "total_count": len(entities),
        }
    except Exception as e:
        logger.error(f"get_map failed: {e}")
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
