"""デルタ通知サービス

check-in時にスナップショットしたtopicスコープに対し、以降追加された
decision/log/materialを差分として取得するための純粋クエリ関数群。
呼び出し元（delta_middleware）がconnとwatermarkを管理し、本モジュールは
DB問い合わせのみを担う。
"""
import sqlite3

LOG_TITLE_SNIPPET_LEN = 50


def material_scope_clause(
    topic_ids: list[int], activity_id: int | None
) -> tuple[str, list[int]]:
    """materialのスコープ条件（topic群 OR activity_id）をSQL断片とパラメータ列で返す。

    topicとactivityは別々のオートインクリメント空間のため、値がたまたま一致しても
    type違いで誤爆しないよう、type毎にid集合をペアで絞り込む（IN列挙をtype横断で
    共有しない）。get_baseline/compute_delta/delta_middleware._scoped_idsの3箇所で
    同一ロジックを使うための共通ヘルパー。

    Returns:
        (sql_fragment, params)。両方とも空になるのはtopic_ids/activity_idが
        いずれも指定されない場合のみで、その場合sql_fragmentは空文字列になる
        （呼び出し側でtruthy判定して使うこと）。
    """
    clauses = []
    params: list[int] = []
    if topic_ids:
        placeholders = ",".join("?" * len(topic_ids))
        clauses.append(f"(rv.source_type = 'topic' AND rv.source_id IN ({placeholders}))")
        params.extend(topic_ids)
    if activity_id is not None:
        clauses.append("(rv.source_type = 'activity' AND rv.source_id = ?)")
        params.append(activity_id)
    return " OR ".join(clauses), params


def get_baseline(
    conn: sqlite3.Connection, topic_ids: list[int], activity_id: int | None = None
) -> dict:
    """指定topic群（decision/log）・topic群+activity_id（material）の現在のmax idを返す。

    materialのスコープをcompute_deltaと揃えてtopic群 **または** activity_idにしている
    （揃えないと、activityにのみ紐づき check-in以前から存在していたmaterialが、
    check-in直後の最初のdelta計算で新規と誤検知される）。
    topic_ids・activity_idがいずれも無ければ全て0を返す（該当なし）。
    retracted_atは考慮しない（baselineは差分の起点となるidカットオフに過ぎず、
    retracted済みかどうかに関係なくidの大小のみが意味を持つため）。

    Returns:
        {"decision_id": int, "log_id": int, "material_id": int}
    """
    decision_id = 0
    log_id = 0

    if topic_ids:
        placeholders = ",".join("?" * len(topic_ids))

        decision_row = conn.execute(
            f"""
            SELECT MAX(d.id) AS max_id
            FROM decisions d
            JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                            AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                            AND r.target_id IN ({placeholders})
            """,
            tuple(topic_ids),
        ).fetchone()
        decision_id = (decision_row["max_id"] if decision_row else None) or 0

        log_row = conn.execute(
            f"""
            SELECT MAX(l.id) AS max_id
            FROM discussion_logs l
            JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                            AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                            AND r.target_id IN ({placeholders})
            """,
            tuple(topic_ids),
        ).fetchone()
        log_id = (log_row["max_id"] if log_row else None) or 0

    material_id = 0
    scope_sql, scope_params = material_scope_clause(topic_ids, activity_id)
    if scope_sql:
        material_row = conn.execute(
            f"""
            SELECT MAX(m.id) AS max_id
            FROM materials m
            JOIN relations_view rv ON ({scope_sql})
                                   AND rv.target_type = 'material' AND rv.target_id = m.id
            """,
            tuple(scope_params),
        ).fetchone()
        material_id = (material_row["max_id"] if material_row else None) or 0

    return {"decision_id": decision_id, "log_id": log_id, "material_id": material_id}


def compute_delta(
    conn: sqlite3.Connection,
    topic_ids: list[int],
    activity_id: int | None,
    wm: dict,
) -> dict:
    """`id > wm[...]` の新規decision/log/materialをtitle付きで返す。

    decision/logはtopic群へのbelongs_toリレーション経由（activity_idは対象外）。
    materialはtopic群 **または** activity_idに関連するものが対象
    （relations_view経由、belongs_to/related問わず）。
    いずれもretracted_at IS NULLが必須（materialも対象。旧設計の
    「materialには付けない」は誤りだったため注意）。

    Args:
        conn: DB接続
        topic_ids: スコープとなるtopic群のID
        activity_id: スコープとなるactivityのID（materialのスコープにのみ使う）
        wm: watermark辞書。少なくとも decision_id/log_id/material_id を持つ

    Returns:
        {"new_decisions": [{"id": int, "title": str}, ...],
         "new_logs": [...], "new_materials": [...]}（空配列可）
    """
    new_decisions: list[dict] = []
    new_logs: list[dict] = []
    new_materials: list[dict] = []

    if topic_ids:
        placeholders = ",".join("?" * len(topic_ids))

        rows = conn.execute(
            f"""
            SELECT DISTINCT d.id, d.title, d.decision
            FROM decisions d
            JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                            AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                            AND r.target_id IN ({placeholders})
            WHERE d.retracted_at IS NULL AND d.id > ?
            ORDER BY d.id
            """,
            (*topic_ids, wm.get("decision_id", 0)),
        ).fetchall()
        new_decisions = [
            {"id": row["id"], "title": row["title"] or row["decision"]} for row in rows
        ]

        rows = conn.execute(
            f"""
            SELECT DISTINCT l.id, l.title, l.content
            FROM discussion_logs l
            JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                            AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                            AND r.target_id IN ({placeholders})
            WHERE l.retracted_at IS NULL AND l.id > ?
            ORDER BY l.id
            """,
            (*topic_ids, wm.get("log_id", 0)),
        ).fetchall()
        new_logs = [
            {"id": row["id"], "title": row["title"] or (row["content"] or "")[:LOG_TITLE_SNIPPET_LEN]}
            for row in rows
        ]

    scope_sql, scope_params = material_scope_clause(topic_ids, activity_id)
    if scope_sql:
        rows = conn.execute(
            f"""
            SELECT DISTINCT m.id, m.title
            FROM materials m
            JOIN relations_view rv ON ({scope_sql})
                                   AND rv.target_type = 'material' AND rv.target_id = m.id
            WHERE m.retracted_at IS NULL AND m.id > ?
            ORDER BY m.id
            """,
            (*scope_params, wm.get("material_id", 0)),
        ).fetchall()
        new_materials = [{"id": row["id"], "title": row["title"]} for row in rows]

    return {
        "new_decisions": new_decisions,
        "new_logs": new_logs,
        "new_materials": new_materials,
    }
