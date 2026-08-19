"""entity write（add_*/update_*/retract/relation/pin）→ relay outbox の core 内部 publish。

relay_publish（セッション向け4動詞の1つ、src.services.relay.service）とは独立した
内部専用 API で、write 系サービス関数の commit 直前に `publish_entity_event_with_conn`
を1行呼ぶだけで完結する。RELAY_BEARER_TOKEN 未設定（relay 未接続）環境では静かに
no-op し、outbox が無限に積もるのを防ぐ。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Literal, Optional

from relay_sdk.config import MAX_TITLE_CHARS
from relay_sdk.outbox import publish as outbox_publish
from src.services.relay import config as relay_config
from src.services.relay.service import PUBLISH_ENTITY_TYPES, validate_labels
from src.services.tag_service import get_entity_tags

logger = logging.getLogger(__name__)

EventType = Literal["created", "updated", "retracted"]

# ref.type として publish 対象になる entity 種別と、対応する DB テーブル名
# （cc-memory 内呼称と完全一致）。
ENTITY_TABLE_MAP: dict[str, str] = {
    "decision": "decisions",
    "log": "discussion_logs",
    "material": "materials",
    "activity": "activities",
    "topic": "discussion_topics",
    "tag": "tags",
    "habit": "habits",
    "ask": "asks",
}
assert set(ENTITY_TABLE_MAP) == set(PUBLISH_ENTITY_TYPES)

# entity 自身の tags 取得に使う junction table（topic/activity/decision/log/material/ask。
# tag/habit 自身は tag 付けされない entity のため対象外）。
_TAG_JUNCTION: dict[str, tuple[str, str]] = {
    "topic": ("topic_tags", "topic_id"),
    "activity": ("activity_tags", "activity_id"),
    "decision": ("decision_tags", "decision_id"),
    "log": ("log_tags", "log_id"),
    "material": ("material_tags", "material_id"),
    "ask": ("ask_tags", "ask_id"),
}

# 1 hop 親方向の ID 参照を relations 経由で引く対象。
# topic は階層の最上位で親を持たないため除外する（除外しないと relations_view の
# 対称展開で「この topic に属する全 entity」を親扱いしてしまい、publish のたびに
# 件数が entity 数に比例して膨張する）。tag/habit は relations テーブルに現れない
# ため、そもそも対象外。
_ONE_HOP_PARENT_LOOKUP_TYPES = frozenset({"activity", "material", "decision", "log"})
_ONE_HOP_PARENT_TARGET_TYPES = ("topic", "activity", "material", "decision", "log")


def _one_hop_parent_labels(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> list[str]:
    """1 hop 親方向の ID を labels として返す（N hop transitive は含めない）。"""
    if entity_type not in _ONE_HOP_PARENT_LOOKUP_TYPES:
        return []
    placeholders = ",".join("?" * len(_ONE_HOP_PARENT_TARGET_TYPES))
    rows = conn.execute(
        f"SELECT DISTINCT target_type, target_id FROM relations_view "
        f"WHERE source_type = ? AND source_id = ? AND relation_type = 'belongs_to' "
        f"AND target_type IN ({placeholders})",
        (entity_type, entity_id, *_ONE_HOP_PARENT_TARGET_TYPES),
    ).fetchall()
    return [f"{row['target_type']}:{row['target_id']}" for row in rows]


def _build_title(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> Optional[str]:
    """entity 種別ごとの取得元から title を組み立てる（200 UTF-8 chars で truncate、publisher 側責務）。"""
    if entity_type == "tag":
        row = conn.execute(
            "SELECT namespace, name FROM tags WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        raw = f"{row['namespace']}:{row['name']}" if row["namespace"] else row["name"]
    elif entity_type == "habit":
        row = conn.execute("SELECT content FROM habits WHERE id = ?", (entity_id,)).fetchone()
        if not row:
            return None
        raw = row["content"] or ""
    elif entity_type == "decision":
        row = conn.execute(
            "SELECT title, decision FROM decisions WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        raw = row["title"] or row["decision"] or ""
    elif entity_type == "log":
        row = conn.execute(
            "SELECT title, content FROM discussion_logs WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        raw = row["title"] or (row["content"] or "")[:50]
    elif entity_type == "ask":
        # asksにはtitleカラムが無いためquestionをそのまま使う（他エンティティのtitle fallbackと同型）。
        row = conn.execute("SELECT question FROM asks WHERE id = ?", (entity_id,)).fetchone()
        if not row:
            return None
        raw = row["question"] or ""
    else:
        table = ENTITY_TABLE_MAP[entity_type]
        row = conn.execute(f"SELECT title FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        if not row:
            return None
        raw = row["title"] or ""
    return raw[:MAX_TITLE_CHARS]


def publish_entity_event_with_conn(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: int,
    event: EventType,
) -> Optional[int]:
    """entity write の commit 直前に呼ぶ、relay outbox への core 内部 publish フック。

    呼び出し元の transaction に乗って outbox 行を INSERT する（commit はしない、
    呼び出し元の commit に委ねる。cc-memory 業務 write と outbox INSERT が同一 tx に
    乗ることで dual-write 問題を構造的に回避する）。

    RELAY_BEARER_TOKEN 未設定（relay 未接続）の環境では静かに no-op する
    （outbox が無限に積もるのを防ぐ。既存の RelayConfigError 判定と同じ config.get_token()
    を使う）。

    Returns:
        outbox 行の id（no-op 時・防御的スキップ時は None）
    """
    if not relay_config.get_token():
        return None
    if entity_type not in ENTITY_TABLE_MAP:
        raise ValueError(f"Unsupported entity_type for entity publish: {entity_type!r}")

    own_tags: list[str] = []
    junction = _TAG_JUNCTION.get(entity_type)
    if junction is not None:
        junction_table, id_column = junction
        own_tags = get_entity_tags(conn, junction_table, id_column, entity_id)

    # 全 entity_type に、自身を指す self label（<type>:<id>）を付与する。
    # これにより個体単位の購読（例: ["activity:1183"]）が「その entity 自身の
    # イベント」にもマッチするようになる（1hop 親 label だけでは子の書き込みにしか
    # マッチしなかった非対称を解消する）。
    self_labels = [f"{entity_type}:{entity_id}"]

    labels = list(dict.fromkeys(
        own_tags
        + [f"entity:{entity_type}", f"event:{event}"]
        + self_labels
        + _one_hop_parent_labels(conn, entity_type, entity_id)
    ))

    message = validate_labels(labels, check_reserved=False)
    if message:
        # labels は内部で組み立てているため通常起こり得ない。write本体を巻き込まない
        # よう、publish だけ諦めてログに残す（防御的フォールバック）。
        logger.error(
            "entity publish labels invalid, skipping publish (entity_type=%s, entity_id=%s): %s",
            entity_type, entity_id, message,
        )
        return None

    title = _build_title(conn, entity_type, entity_id)

    return outbox_publish(conn, ref_type=entity_type, ref_id=entity_id, labels=labels, title=title)


# updated_at カラムを実際に持つ entity 種別。2026-07 時点で activities / materials の
# 2 テーブルのみが該当し、decisions / discussion_logs / discussion_topics / tags /
# habits には updated_at カラムが無い。
_HAS_UPDATED_AT_COLUMN = frozenset({"activity", "material"})


def bump_updated_at_and_publish_with_conn(
    conn: sqlite3.Connection, entity_type: str, entity_id: int
) -> Optional[int]:
    """relation/pin の add/remove 用: entity の updated_at を（カラムがあれば）進めて
    event:updated を publish する。

    updated_at カラムは activities/materials にしか存在しないため、他の entity
    種別は UPDATE 文をスキップし publish のみ行う。
    """
    if entity_type in _HAS_UPDATED_AT_COLUMN:
        table = ENTITY_TABLE_MAP[entity_type]
        conn.execute(
            f"UPDATE {table} SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (entity_id,)
        )
    return publish_entity_event_with_conn(
        conn, entity_type=entity_type, entity_id=entity_id, event="updated"
    )
