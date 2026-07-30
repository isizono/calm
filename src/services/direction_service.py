"""方向性 decision（`layer:direction` タグ）の非ランク網羅列挙。

人間が出す抽象方向性の判断は判例が効かない前例なし領域の裁定であり、一般 decision と
区別して少数・明示・supersede 管理で保持する。方向性 decision の物理的な置き場は議論が
起きた通常の topic のままとし、本モジュールが「常にここから網羅的に引ける」という置き場
の実体を提供する。

`layer:direction` タグは decision に直付けされたときのみ方向性 decision として扱う
（トピック経由の継承タグでは直接紐付け判定は成立しない。domain 絞り込みのみトピック
継承を考慮する）。

この列挙は現状 MCP ツールとしては公開しておらず、受動的な露出のみを持つ:
add_decisions が `layer:direction` item 作成時にレスポンスへ同 domain の既存方向性
decision を同梱する経路と、hint_service が overflow 閾値を件数判定する経路の 2 つ。
エージェントが能動的に一覧取得する経路はまだ無い。
"""
import sqlite3

from src.services.staleness_service import annotate_staleness
from src.services.supersede_service import compute_supersede_info_batch
from src.services.tag_service import get_effective_tags_batch_by_ids

DIRECTION_NAMESPACE = "layer"
DIRECTION_NAME = "direction"


def get_direction_tag_id(conn: sqlite3.Connection) -> int | None:
    """`layer:direction` タグの id を返す。タグが一度も作成されていなければ None。"""
    row = conn.execute(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?",
        (DIRECTION_NAMESPACE, DIRECTION_NAME),
    ).fetchone()
    return row["id"] if row else None


def _domain_filter_clause(domain_tag_ids: list[int]) -> str:
    """domain 絞り込みの WHERE 追加句を返す。

    直付け domain タグ OR 親 topic 継承 domain タグのいずれかが一致する decision に絞る。
    プレースホルダは domain_tag_ids を 2 回展開する前提（直付け用と topic 継承用の 2 箇所）。
    """
    placeholders = ",".join("?" * len(domain_tag_ids))
    return f"""
      AND (
        EXISTS (
            SELECT 1 FROM decision_tags dt2
            WHERE dt2.decision_id = d.id AND dt2.tag_id IN ({placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM relations r
            JOIN topic_tags tt ON tt.topic_id = r.target_id
            WHERE r.source_type = 'decision' AND r.source_id = d.id
              AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
              AND tt.tag_id IN ({placeholders})
        )
      )
    """


def get_direction_decisions(
    conn: sqlite3.Connection,
    domain_tag_ids: list[int] | None = None,
    include_superseded: bool = False,
) -> list[dict]:
    """有効な方向性 decision を非ランクで網羅列挙する。

    Args:
        conn: DB 接続。
        domain_tag_ids: 指定時、直付け domain タグ OR 親 topic 継承 domain タグの
            いずれかが一致する decision に絞る（OR 条件）。空リスト/None は絞り込みなし。
        include_superseded: True のとき supersede 済みも含める。デフォルトは
            active（非 supersede）のみを返す。

    Returns:
        created_at 昇順（古い方向性が先。基盤ほど先頭）のリスト。各要素:
        {id, title, decision, reason, tags, created_at, staleness}
        staleness は staleness_service.annotate_staleness が付与する形。
        retract 済みは常に除外する。
    """
    direction_tag_id = get_direction_tag_id(conn)
    if direction_tag_id is None:
        return []

    sql = """
        SELECT d.* FROM decisions d
        JOIN decision_tags dt ON dt.decision_id = d.id AND dt.tag_id = ?
        WHERE d.retracted_at IS NULL
    """
    params: list = [direction_tag_id]

    if domain_tag_ids:
        sql += _domain_filter_clause(domain_tag_ids)
        params.extend(domain_tag_ids)
        params.extend(domain_tag_ids)

    sql += " ORDER BY d.created_at ASC, d.id ASC"

    rows = conn.execute(sql, tuple(params)).fetchall()
    decisions = [dict(row) for row in rows]
    if not decisions:
        return []

    if not include_superseded:
        decision_ids = [d["id"] for d in decisions]
        supersede_map = compute_supersede_info_batch(conn, decision_ids)
        decisions = [
            d for d in decisions
            if not supersede_map.get(d["id"], {"is_superseded": False})["is_superseded"]
        ]
        if not decisions:
            return []

    decision_ids = [d["id"] for d in decisions]
    tags_map = get_effective_tags_batch_by_ids(conn, "decision", decision_ids)

    items = []
    for d in decisions:
        items.append({
            "id": d["id"],
            "title": d.get("title") or (d["decision"] or "")[:50],
            "decision": d["decision"],
            "reason": d["reason"],
            "tags": tags_map.get(d["id"], []),
            "created_at": d["created_at"],
        })

    annotate_staleness(conn, items)
    return items


def count_direction_decisions(
    conn: sqlite3.Connection,
    domain_tag_ids: list[int] | None = None,
) -> int:
    """有効な（非 supersede・非 retract）方向性 decision の件数を COUNT で返す軽量版。

    get_direction_decisions と同じ絞り込み条件だが、本文・タグ・staleness の解決や
    supersede chain の構築を一切行わない。件数の閾値判定だけが必要な経路で使う。

    supersede 判定は「その decision を target とする decision_supersedes 行が存在するか」で
    行う。compute_supersede_info_batch の is_superseded は newer 方向（target→source）へ
    1 ホップでも到達できれば True になるため、EXISTS による直接判定と等価。

    Args:
        conn: DB 接続。
        domain_tag_ids: 指定時、直付け domain タグ OR 親 topic 継承 domain タグの
            いずれかが一致する decision に絞る（OR 条件）。空リスト/None は絞り込みなし。
    """
    direction_tag_id = get_direction_tag_id(conn)
    if direction_tag_id is None:
        return 0

    sql = """
        SELECT COUNT(*) AS n FROM decisions d
        JOIN decision_tags dt ON dt.decision_id = d.id AND dt.tag_id = ?
        WHERE d.retracted_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM decision_supersedes ds WHERE ds.target_id = d.id AND ds.kind = 'replaces'
          )
    """
    params: list = [direction_tag_id]

    if domain_tag_ids:
        sql += _domain_filter_clause(domain_tag_ids)
        params.extend(domain_tag_ids)
        params.extend(domain_tag_ids)

    row = conn.execute(sql, tuple(params)).fetchone()
    return row["n"] if row else 0
