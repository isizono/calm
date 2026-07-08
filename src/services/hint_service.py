"""HintService: 想起させたい意図を単一窓口に集約する

`get_hints(scope, target_id) -> list[Hint]` 統一API。
hintの種別と発火条件は仕様確定decisionに従う。

種別:
- recompose_bootstrap (immediate): tag scope, domain: namespaceのみ, decision累計 ≥ 30
- recompose_delta (immediate): tag scope, domain: namespaceのみ, pinned material以降の増分 ≥ 100
- logs_sparse (deferred): topic scope, log < 5 かつ decision > 0
- follow_up_after_decision (deferred): 直近turnでadd_decisions単独、他記録系なし
- record_missing (deferred): 一定turn記録系ツール未呼出
- direction_overflow (immediate): tag scope, domain: namespaceのみ,
  layer:direction decisionのactive件数 ≥ DIRECTION_OVERFLOW_THRESHOLD

抑制:
- tag_notesに以下のハッシュタグマーカーがあれば該当hintをスキップ:
  #recompose-skipped, #recompose-bootstrap-skipped, #recompose-delta-skipped,
  #logs-sparse-ack, #direction-overflow-ack
- 各マーカーは素の形（恒久抑制）に加えて `<marker>-until:YYYY-MM-DD` の形式
  （指定日当日まで有効な期限付き抑制）も受け付ける。不正な日付形式は無視される
  （フェイルオープン、抑制しない側に倒す）。判定は_is_marker_activeに集約する
- recompose_bootstrap / recompose_deltaはhintが実際に生成される都度、対象tagの
  notesへ `<marker>-until:{today}` を自動追記する日次クールダウンを持つ（同日内の
  再発火を防ぐ）。既存の日付付きマーカーが未来日の場合は上書きしない（手動設定の
  長期抑制を優先する）。書き込みは_apply_cooldown_markerに集約する
- direction_overflowはこの日次クールダウンの対象外（手動マーカーのみで抑制する）
- orch-managed activityでの全suppressは呼出側責務 (本moduleは判定しない)

severity値域: info | warn のみ (block不採用)

follow_up_after_decision / record_missing はevents.jsonl状態が必要なため
本moduleでは判定しない。Stop hookが既存ロジックで生成し、type名のみ統一する。

Edge cases (フェイルオープン):
- target_id不在/scope型不一致 → 空リスト
- DB失敗 → 空リスト + stderrログ
"""

import re
import sqlite3
import sys
from datetime import date
from typing import Any, Literal, TypedDict

from src.config import DIRECTION_OVERFLOW_THRESHOLD
from src.db import get_connection
from src.services.direction_service import count_direction_decisions
from src.services.tag_service import _set_tag_notes_by_id_with_conn

# --- 型定義 ---

HintType = Literal[
    "recompose_bootstrap",
    "recompose_delta",
    "logs_sparse",
    "follow_up_after_decision",
    "record_missing",
    "direction_overflow",
]
Severity = Literal["info", "warn"]
DeliveryHint = Literal["immediate", "deferred"]
Scope = Literal["tag", "topic", "activity"]


class SuggestedAction(TypedDict, total=False):
    tool: str | None
    skill: str | None
    args_hint: dict[str, Any] | None
    natural_language: str


class Hint(TypedDict):
    type: HintType
    severity: Severity
    message: str
    suggested_action: SuggestedAction
    source: str
    delivery_hint: DeliveryHint


# --- 閾値 ---

RECOMPOSE_BOOTSTRAP_THRESHOLD = 30
RECOMPOSE_DELTA_THRESHOLD = 100
LOGS_SPARSE_LOG_THRESHOLD = 5


# --- 抑制マーカー ---

MARKER_RECOMPOSE_GENERIC = "#recompose-skipped"
MARKER_RECOMPOSE_BOOTSTRAP = "#recompose-bootstrap-skipped"
MARKER_RECOMPOSE_DELTA = "#recompose-delta-skipped"
MARKER_LOGS_SPARSE = "#logs-sparse-ack"
MARKER_DIRECTION_OVERFLOW = "#direction-overflow-ack"

# マーカーは素の形（恒久抑制）に加えて `<marker>-until:YYYY-MM-DD`（期限付き抑制、
# 指定日当日まで有効）も受け付ける。判定は_is_marker_activeに集約する。
_DATED_MARKER_SUFFIX = "-until:"


def _dated_marker_pattern(marker: str) -> re.Pattern[str]:
    return re.compile(re.escape(marker) + re.escape(_DATED_MARKER_SUFFIX) + r"(\d{4}-\d{2}-\d{2})")


def _is_marker_active(notes: str, marker: str) -> bool:
    """notes内でmarkerが有効な抑制指示として存在するかを判定する。

    2形態をサポートする:
    - 素のmarker（例: "#recompose-skipped"）: 恒久抑制
    - 日付付き（例: "#recompose-skipped-until:2026-08-01"）: date.today() <= 指定日
      の間だけ抑制。不正な日付形式は無視する（フェイルオープン、抑制しない側に倒す）

    日付付きの出現は素のmarkerを部分文字列として含む（prefix関係）ため、日付付き
    パターンを先に検出して本文から除去してから素のmarker判定を行う。これを怠ると
    期限切れの日付付きマーカーが恒久マーカーとして誤って残り続けてしまう。
    """
    pattern = _dated_marker_pattern(marker)
    for date_str in pattern.findall(notes):
        try:
            until = date.fromisoformat(date_str)
        except ValueError:
            continue
        if date.today() <= until:
            return True
    remainder = pattern.sub("", notes)
    return marker in remainder


def _merge_cooldown_marker(notes: str, marker: str, today: date) -> str:
    """notesに対してmarkerの日次クールダウンマーカーを解析し、更新後のnotesを返す。

    既存の日付付きマーカー（`<marker>-until:YYYY-MM-DD`）がtodayより後（未来）で
    あれば、ユーザーが意図的に設定した長期抑制とみなしnotesをそのまま返す（no-op）。
    存在しない、不正な日付形式、またはtoday以前の日付であれば、既存の日付付き
    マーカーを除去したうえでtoday基準のマーカーを追記する（更新は「除去→末尾追記」
    で実現し、出現位置は保持しない）。素の恒久マーカーの扱いは呼び出し元の責務
    （hintが生成された時点で恒久抑制は既に効いていないことが前提）。
    """
    pattern = _dated_marker_pattern(marker)
    for date_str in pattern.findall(notes):
        try:
            until = date.fromisoformat(date_str)
        except ValueError:
            continue
        if until > today:
            return notes
    remainder = pattern.sub("", notes).strip()
    new_marker = f"{marker}{_DATED_MARKER_SUFFIX}{today.isoformat()}"
    return f"{remainder}\n\n{new_marker}" if remainder else new_marker


def _apply_cooldown_marker(
    conn: sqlite3.Connection, tag_id: int, notes: str, marker: str
) -> str:
    """hint発火に伴い、対象tagのnotesへ日次クールダウンマーカーを書き込む。

    _merge_cooldown_markerがno-opと判定した場合はDB書き込みを行わない。
    書き込みが発生した場合のみcommitする。呼び出し元（_get_hints_for_tag）は
    後続の判定で更新後のnotesを参照する必要があるため、返り値を使い回すこと。
    """
    new_notes = _merge_cooldown_marker(notes, marker, date.today())
    if new_notes != notes:
        _set_tag_notes_by_id_with_conn(conn, tag_id, new_notes)
        conn.commit()
    return new_notes


# --- 文言 ---

HINT_LOGS_SPARSE_MESSAGE = (
    "このトピックはdecisionsに対してlogsが少ないです。"
    "議論の経緯をadd_logsで記録してください。"
    "決定事項だけでは、なぜその結論に至ったかが将来のセッションで失われます。"
    f"今は都合が悪い場合、対象topicのdomain:タグのnotesに"
    f"「{MARKER_LOGS_SPARSE}-until:YYYY-MM-DD」（任意の未来日）を追記すると、"
    f"その日まで一時的に黙らせられます。恒久的に不要なら日付なしの"
    f"「{MARKER_LOGS_SPARSE}」を追記してください。"
)


RECOMPOSE_AUTOTRIGGER_GUARD = (
    "別セッションで実施しても構わない旨をユーザーに伝えてください。"
    "ユーザーがこのセッションでの実施を明示的に求めない限り、"
    "このセッションでrecompose-context skillを自発的に実行してはいけません。"
)


def _recompose_bootstrap_message(tag_name: str, total_count: int) -> str:
    return (
        f"tag「{tag_name}」にdecisionが{total_count}件蓄積していますが、"
        f"統合material（recomposed material）がありません。"
        f"recompose-context skillでの初回整理をユーザーに提案してください。"
        f"{RECOMPOSE_AUTOTRIGGER_GUARD}"
        f"今は都合が悪い場合、tag notesに"
        f"「{MARKER_RECOMPOSE_BOOTSTRAP}-until:YYYY-MM-DD」（任意の未来日）を"
        f"追記すると、その日まで一時的に黙らせられます。恒久的に不要なら日付なしの"
        f"「{MARKER_RECOMPOSE_BOOTSTRAP}」を追記してください。"
    )


def _recompose_delta_message(tag_name: str, delta_count: int) -> str:
    return (
        f"tag「{tag_name}」はrecomposed materialの最終更新以降にdecisionが"
        f"{delta_count}件増えています。recompose-context skillでのメンテを"
        f"ユーザーに提案してください。"
        f"{RECOMPOSE_AUTOTRIGGER_GUARD}"
        f"今は都合が悪い場合、tag notesに"
        f"「{MARKER_RECOMPOSE_DELTA}-until:YYYY-MM-DD」（任意の未来日）を"
        f"追記すると、その日まで一時的に黙らせられます。恒久的に不要なら日付なしの"
        f"「{MARKER_RECOMPOSE_DELTA}」を追記してください。"
    )


def _direction_overflow_message(tag_name: str, count: int) -> str:
    return (
        f"tag「{tag_name}」の有効な方向性decision（layer:direction）が{count}件"
        f"あります。少数原則の維持のため、統合・supersede整理をユーザーに提案してください。"
        f"今は都合が悪い場合、tag notesに"
        f"「{MARKER_DIRECTION_OVERFLOW}-until:YYYY-MM-DD」（任意の未来日）を"
        f"追記すると、その日まで一時的に黙らせられます。恒久的に不要なら日付なしの"
        f"「{MARKER_DIRECTION_OVERFLOW}」を追記してください。"
    )


# --- 公開API ---


def is_orch_managed_activity(conn: sqlite3.Connection, activity_id: int) -> bool:
    """activityがorch管理かを判定する。

    activities.orch_managed カラムを参照する。
    orch-managed activityでは全hint suppressとする呼出側ガードに使う。
    存在しない activity_id は False を返す（フェイルオープン）。
    """
    row = conn.execute(
        "SELECT orch_managed FROM activities WHERE id = ?",
        (activity_id,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["orch_managed"])


def get_hints(scope: Scope, target_id: int) -> list[Hint]:
    """指定scope/target_idに該当するhintを返す。

    呼出側はorch-managed等のsuppress判定を別途行うこと。本moduleはDB状態のみで判定する。
    """
    try:
        conn = get_connection()
        try:
            return get_hints_with_conn(conn, scope, target_id)
        finally:
            conn.close()
    except Exception as e:
        print(f"hint_service.get_hints error: {e}", file=sys.stderr)
        return []


def get_hints_with_conn(
    conn: sqlite3.Connection, scope: Scope, target_id: int
) -> list[Hint]:
    """conn共有版。サービス間呼び出し用。"""
    if scope == "tag":
        return _get_hints_for_tag(conn, target_id)
    if scope == "topic":
        return _get_hints_for_topic(conn, target_id)
    if scope == "activity":
        return _get_hints_for_activity(conn, target_id)
    return []


# --- scope=tag ---


def _get_hints_for_tag(conn: sqlite3.Connection, tag_id: int) -> list[Hint]:
    """tagに対するrecompose_bootstrap/recompose_delta/direction_overflow判定。

    対象tagはdomain: namespaceに限定する。
    """
    tag_row = conn.execute(
        "SELECT id, namespace, name, notes FROM tags WHERE id = ?",
        (tag_id,),
    ).fetchone()
    if tag_row is None:
        return []
    if tag_row["namespace"] != "domain":
        return []

    notes = tag_row["notes"] or ""
    tag_name = f"{tag_row['namespace']}:{tag_row['name']}"
    hints: list[Hint] = []

    base_time = _get_pinned_material_max_time(conn, tag_id)
    if base_time is not None:
        if not (_is_marker_active(notes, MARKER_RECOMPOSE_GENERIC)
                or _is_marker_active(notes, MARKER_RECOMPOSE_DELTA)):
            delta = _count_tag_scope_decisions(conn, tag_id, after=base_time)
            if delta >= RECOMPOSE_DELTA_THRESHOLD:
                hints.append({
                    "type": "recompose_delta",
                    "severity": "info",
                    "message": _recompose_delta_message(tag_name, delta),
                    "suggested_action": {
                        "skill": "recompose-context",
                        "args_hint": {"tag": tag_name},
                        "natural_language": (
                            f"tag「{tag_name}」のrecompose-context skillでメンテを提案する"
                        ),
                    },
                    "source": f"recompose_delta:tag:{tag_id}",
                    "delivery_hint": "immediate",
                })
                notes = _apply_cooldown_marker(conn, tag_id, notes, MARKER_RECOMPOSE_DELTA)
    else:
        if not (_is_marker_active(notes, MARKER_RECOMPOSE_GENERIC)
                or _is_marker_active(notes, MARKER_RECOMPOSE_BOOTSTRAP)):
            total = _count_tag_scope_decisions(conn, tag_id)
            if total >= RECOMPOSE_BOOTSTRAP_THRESHOLD:
                hints.append({
                    "type": "recompose_bootstrap",
                    "severity": "info",
                    "message": _recompose_bootstrap_message(tag_name, total),
                    "suggested_action": {
                        "skill": "recompose-context",
                        "args_hint": {"tag": tag_name},
                        "natural_language": (
                            f"tag「{tag_name}」の初回統合をrecompose-context skillで提案する"
                        ),
                    },
                    "source": f"recompose_bootstrap:tag:{tag_id}",
                    "delivery_hint": "immediate",
                })
                notes = _apply_cooldown_marker(conn, tag_id, notes, MARKER_RECOMPOSE_BOOTSTRAP)

    if not _is_marker_active(notes, MARKER_DIRECTION_OVERFLOW):
        direction_count = count_direction_decisions(conn, domain_tag_ids=[tag_id])
        if direction_count >= DIRECTION_OVERFLOW_THRESHOLD:
            hints.append({
                "type": "direction_overflow",
                "severity": "info",
                "message": _direction_overflow_message(tag_name, direction_count),
                "suggested_action": {
                    "natural_language": (
                        f"tag「{tag_name}」の方向性decisionの統合・supersede整理を"
                        "ユーザーに提案する"
                    ),
                },
                "source": f"direction_overflow:tag:{tag_id}",
                "delivery_hint": "immediate",
            })

    return hints


def _get_pinned_material_max_time(
    conn: sqlite3.Connection, tag_id: int
) -> str | None:
    """tagにpinされたmaterialの最終更新時刻T (複数あればmax)。"""
    # MAX() は集約関数なのでマッチ行ゼロでも1行 (t=NULL) を返す。
    # よって row[\"t\"] が None の場合がpin不在を表す。
    row = conn.execute(
        """
        SELECT MAX(COALESCE(m.updated_at, m.created_at)) AS t
        FROM pins p
        JOIN materials m ON m.id = p.target_id
        WHERE p.source_type = 'tag' AND p.source_id = ?
          AND p.target_type = 'material'
        """,
        (tag_id,),
    ).fetchone()
    return row["t"] if row else None


def _count_tag_scope_decisions(
    conn: sqlite3.Connection, tag_id: int, after: str | None = None
) -> int:
    """tagスコープのdecision件数 (retracted除外)。

    tagスコープは decision_tags 直付け OR relations.belongs_to 経由で
    親 topic の topic_tags 継承の和。
    """
    sql = """
        SELECT COUNT(*) FROM decisions d
        WHERE d.retracted_at IS NULL
          AND (
            EXISTS (
                SELECT 1 FROM decision_tags dt
                WHERE dt.decision_id = d.id AND dt.tag_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM relations r
                JOIN topic_tags tt ON tt.topic_id = r.target_id
                WHERE r.source_type = 'decision' AND r.source_id = d.id
                  AND r.target_type = 'topic'
                  AND r.relation_type = 'belongs_to'
                  AND tt.tag_id = ?
            )
          )
    """
    params: list = [tag_id, tag_id]
    if after is not None:
        sql += " AND d.created_at > ?"
        params.append(after)
    row = conn.execute(sql, tuple(params)).fetchone()
    return row[0] if row else 0


# --- scope=topic ---


def _get_hints_for_topic(conn: sqlite3.Connection, topic_id: int) -> list[Hint]:
    """topicに対するlogs_sparse判定 (素朴閾値: log<5 かつ decision>0)。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM decisions d
        JOIN relations r
          ON r.source_type = 'decision' AND r.source_id = d.id
         AND r.target_type = 'topic'
         AND r.relation_type = 'belongs_to'
         AND r.target_id = ?
        WHERE d.retracted_at IS NULL
        """,
        (topic_id,),
    ).fetchone()
    decision_count = row["cnt"] if row else 0
    if decision_count == 0:
        return []

    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM discussion_logs dl
        JOIN relations r
          ON r.source_type = 'log' AND r.source_id = dl.id
         AND r.target_type = 'topic'
         AND r.relation_type = 'belongs_to'
         AND r.target_id = ?
        WHERE dl.retracted_at IS NULL
        """,
        (topic_id,),
    ).fetchone()
    log_count = row["cnt"] if row else 0
    if log_count >= LOGS_SPARSE_LOG_THRESHOLD:
        return []

    for notes in _get_topic_domain_tag_notes(conn, topic_id):
        if _is_marker_active(notes, MARKER_LOGS_SPARSE):
            return []

    return [{
        "type": "logs_sparse",
        "severity": "info",
        "message": HINT_LOGS_SPARSE_MESSAGE,
        "suggested_action": {
            "tool": "add_logs",
            "args_hint": {"topic_id": topic_id},
            "natural_language": "議論の経緯をadd_logsで記録するようユーザーに提案する",
        },
        "source": f"logs_sparse:topic:{topic_id}",
        "delivery_hint": "deferred",
    }]


def _get_topic_domain_tag_notes(
    conn: sqlite3.Connection, topic_id: int
) -> list[str]:
    """topicに紐づくdomain:タグのnotesを返す。

    抑制マーカーはdomain:タグの notes に書く設計。よって同じdomain:タグを持つ
    別topicにも suppress が波及することは仕様で、軽量記述を優先した結果である
    (トピック単位の抑制は意図的にサポートしない)。
    """
    rows = conn.execute(
        """
        SELECT t.notes FROM tags t
        JOIN topic_tags tt ON tt.tag_id = t.id
        WHERE tt.topic_id = ? AND t.namespace = 'domain' AND t.notes IS NOT NULL
        """,
        (topic_id,),
    ).fetchall()
    return [r["notes"] for r in rows]


# --- scope=activity ---


def _get_hints_for_activity(
    conn: sqlite3.Connection, activity_id: int
) -> list[Hint]:
    """activityに紐づくdomain:tagを展開してrecompose系hintを集約する。

    activityの所属tagのうちdomain:namespaceのみ対象 (D#2780)。
    """
    rows = conn.execute(
        """
        SELECT t.id FROM tags t
        JOIN activity_tags at ON at.tag_id = t.id
        WHERE at.activity_id = ? AND t.namespace = 'domain'
        """,
        (activity_id,),
    ).fetchall()
    hints: list[Hint] = []
    seen_sources: set[str] = set()
    for r in rows:
        for hint in _get_hints_for_tag(conn, r["id"]):
            if hint["source"] not in seen_sources:
                seen_sources.add(hint["source"])
                hints.append(hint)
    return hints
