"""デルタ通知サービス

topicスコープを引き直しつつ、以降追加されたdecision/log/materialを差分として
取得するための純粋クエリ関数群。scopeは購読テーブルとして永続化せず、
呼び出し元（delta_middleware）がツール呼び出しのたびにderive_scopeで引き直す。
呼び出し元がconnとwatermarkを管理し、本モジュールはDB問い合わせのみを担う。
"""
import sqlite3

LOG_TITLE_SNIPPET_LEN = 50


def derive_scope(conn: sqlite3.Connection, activity_id: int) -> list[int]:
    """activity_idから差分通知のtopicスコープをその場で引き直す。

    スコープは購読状態として保存せず、呼び出しのたびに次の2つの和として計算する。
      - activity_idから1段の関連にあるtopic
      - それらのtopicに関連する、素タグ `board`（namespace=''）の付いたtopic

    2段目をboardタグで絞るのは、topic同士の関連をタグ無しでたどると、関係の薄い
    topicの記録まで毎回のスコープに混ざるため。

    Returns:
        topic_idのリスト（重複なし、順不同）。activity_idに直接関連するtopicが
        無ければ空リスト。
    """
    rows = conn.execute(
        """
        WITH direct(topic_id) AS (
            SELECT target_id FROM relations_view
            WHERE source_type = 'activity' AND source_id = ? AND target_type = 'topic'
        )
        SELECT topic_id FROM direct
        UNION
        SELECT rv.target_id
        FROM relations_view rv
        JOIN direct d ON rv.source_type = 'topic' AND rv.source_id = d.topic_id
        JOIN topic_tags tt ON tt.topic_id = rv.target_id
        JOIN tags t ON t.id = tt.tag_id AND t.namespace = '' AND t.name = 'board'
        WHERE rv.target_type = 'topic'
        """,
        (activity_id,),
    ).fetchall()
    return [row["topic_id"] for row in rows]


def material_scope_clause(
    topic_ids: list[int], activity_id: int | None
) -> tuple[str, list[int]]:
    """materialのスコープ条件（topic群 OR activity_id）をSQL断片とパラメータ列で返す。

    topicとactivityは別々のオートインクリメント空間のため、値がたまたま一致しても
    type違いで誤爆しないよう、type毎にid集合をペアで絞り込む（IN列挙をtype横断で
    共有しない）。compute_delta/delta_middleware._scoped_idsの2箇所で同一ロジックを
    使うための共通ヘルパー。

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


def get_baseline(conn: sqlite3.Connection) -> dict:
    """decision/log/materialの現在の全体max id（scopeに関係なく）を返す。

    差分通知の既読位置の初期値は、check-in時点のscopeにおけるmaxではなく、
    テーブル全体のmaxを使う。scopeをツール呼び出しのたびに引き直す設計のもとでは、
    scopeが後から広がったとき（例: topicへboardタグ付きtopicが新たに関連付けられた
    とき）、新しく入ったtopicの古い項目がまとめて新規として検出されるのを防ぐ
    （scope拡大より前に存在した項目は、topicに関係なく全てこのmaxのid以下になる）。

    scopeが変化しない限り、scope内maxを使った場合と結果は変わらない。idは
    テーブルごとに作成順で単調増加するため、「check-in以降に作られたか」という
    判定はtopicの内外に関係なく成立し、scopeによる絞り込みはcompute_delta側の
    topic条件だけで担保される。

    retracted_atは考慮しない（baselineは差分の起点となるidカットオフに過ぎず、
    retracted済みかどうかに関係なくidの大小のみが意味を持つため）。

    Returns:
        {"decision_id": int, "log_id": int, "material_id": int}
    """
    decision_row = conn.execute("SELECT MAX(id) AS max_id FROM decisions").fetchone()
    log_row = conn.execute("SELECT MAX(id) AS max_id FROM discussion_logs").fetchone()
    material_row = conn.execute("SELECT MAX(id) AS max_id FROM materials").fetchone()
    return {
        "decision_id": (decision_row["max_id"] if decision_row else None) or 0,
        "log_id": (log_row["max_id"] if log_row else None) or 0,
        "material_id": (material_row["max_id"] if material_row else None) or 0,
    }


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
