"""人の訂正を「未教訓化」として持ち越す仕組みの読み取り側。

記録役が人の訂正をmaterial（素タグ`unlearned-correction`）として積む。その件は、
次の2つが揃うまで「未解消」として持ち越される。
- 届け先: 次にどこから届くか（置き場と発火の契機）を書いた記録（素タグ`lesson-delivery`）
- 観測: 届いたことを書き手以外が確かめた記録（素タグ`lesson-observed`）
どちらもlogかmaterialで、未教訓化のmaterialとadd_relationで結ばれていればよい。
skill・rulesのように届いたことを観測できない置き場に書いたときは、届け先の記録に
素タグ`lesson-unobservable`も付ける。その件は未解消の一覧から外れるが、解消とは
数えず「観測なし」として別に数える（混ぜると観測できない件で解消率が水増しされる）。

同じ型の未教訓化は、記録役が積むときに前の件とadd_relationで結ぶ。結ばれた件のうち、
同じ型の他の誤りを洗った結果（素タグ`same-type-checked`）がまだ結ばれていないものは
「同型の点検待ち」として出す。

hooksからもimportするため、src.db以外の重い依存を持たない。
"""
import sqlite3

# hooks/recorder_instructions.md・scripts/corrections.pyと同名。
UNLEARNED_CORRECTION_TAG = "unlearned-correction"
LESSON_DELIVERY_TAG = "lesson-delivery"
LESSON_OBSERVED_TAG = "lesson-observed"
LESSON_UNOBSERVABLE_TAG = "lesson-unobservable"
SAME_TYPE_CHECKED_TAG = "same-type-checked"

_LINKED_TAG_EXISTS = """EXISTS (
    SELECT 1 FROM relations_view r
    WHERE r.source_type = 'material' AND r.source_id = m.id
      AND (
        (r.target_type = 'log' AND EXISTS (
            SELECT 1 FROM discussion_logs l
            JOIN log_tags lt ON lt.log_id = l.id JOIN tags t ON t.id = lt.tag_id
            WHERE l.id = r.target_id AND l.retracted_at IS NULL
              AND t.namespace = '' AND t.name = ?))
        OR (r.target_type = 'material' AND EXISTS (
            SELECT 1 FROM materials m2
            JOIN material_tags mt2 ON mt2.material_id = m2.id JOIN tags t ON t.id = mt2.tag_id
            WHERE m2.id = r.target_id AND m2.retracted_at IS NULL
              AND t.namespace = '' AND t.name = ?))
      ))"""

_IS_CORRECTION = """m.retracted_at IS NULL
    AND EXISTS (
        SELECT 1 FROM material_tags mt JOIN tags t ON t.id = mt.tag_id
        WHERE mt.material_id = m.id AND t.namespace = '' AND t.name = ?)"""


def unresolved_corrections(
    conn: sqlite3.Connection, activity_id: int, topic_ids: list[int], limit: int
) -> tuple[list[dict], int]:
    """activityに直接つながる、またはtopic_idsのtopicに属する、未解消の未教訓化を返す。

    戻り値は(古い順の先頭limit件の{id, title, delivered, observed}, 未解消の総数)。
    古い順にするのは、持ち越しが長いものほど先に片付けさせるため。
    """
    topic_clause = ""
    params: list = [UNLEARNED_CORRECTION_TAG, activity_id]
    if topic_ids:
        topic_clause = f" OR (rv.target_type = 'topic' AND rv.target_id IN ({','.join('?' * len(topic_ids))}))"
        params += topic_ids
    where = f"""WHERE {_IS_CORRECTION}
        AND EXISTS (
            SELECT 1 FROM relations_view rv
            WHERE rv.source_type = 'material' AND rv.source_id = m.id
              AND ((rv.target_type = 'activity' AND rv.target_id = ?){topic_clause}))
        AND NOT ({_LINKED_TAG_EXISTS} AND ({_LINKED_TAG_EXISTS} OR {_LINKED_TAG_EXISTS}))"""
    params += [LESSON_DELIVERY_TAG] * 2 + [LESSON_OBSERVED_TAG] * 2 + [LESSON_UNOBSERVABLE_TAG] * 2
    total = conn.execute(f"SELECT COUNT(*) FROM materials m {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT m.id, m.title,
                   {_LINKED_TAG_EXISTS} AS delivered,
                   {_LINKED_TAG_EXISTS} AS observed
            FROM materials m {where}
            ORDER BY m.created_at, m.id LIMIT ?""",
        [LESSON_DELIVERY_TAG] * 2 + [LESSON_OBSERVED_TAG] * 2 + params + [limit],
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "delivered": bool(r[2]), "observed": bool(r[3])} for r in rows
    ], total


def same_type_pending(conn: sqlite3.Connection, activity_id: int, topic_ids: list[int], limit: int) -> list[int]:
    """スコープ内の未教訓化のうち、同じ型の別の件と結ばれ、まだ点検の結果が結ばれていないもののid。"""
    topic_clause = ""
    params: list = [UNLEARNED_CORRECTION_TAG, activity_id]
    if topic_ids:
        topic_clause = f" OR (rv.target_type = 'topic' AND rv.target_id IN ({','.join('?' * len(topic_ids))}))"
        params += topic_ids
    rows = conn.execute(
        f"""SELECT m.id FROM materials m
            WHERE {_IS_CORRECTION}
              AND EXISTS (
                SELECT 1 FROM relations_view rv
                WHERE rv.source_type = 'material' AND rv.source_id = m.id
                  AND ((rv.target_type = 'activity' AND rv.target_id = ?){topic_clause}))
              AND EXISTS (
                SELECT 1 FROM relations_view rs
                JOIN materials o ON o.id = rs.target_id AND o.retracted_at IS NULL
                JOIN material_tags ot ON ot.material_id = o.id JOIN tags t ON t.id = ot.tag_id
                WHERE rs.source_type = 'material' AND rs.source_id = m.id AND rs.target_type = 'material'
                  AND t.namespace = '' AND t.name = ?)
              AND NOT {_LINKED_TAG_EXISTS}
            ORDER BY m.created_at, m.id LIMIT ?""",
        params + [UNLEARNED_CORRECTION_TAG, SAME_TYPE_CHECKED_TAG, SAME_TYPE_CHECKED_TAG, limit],
    ).fetchall()
    return [r[0] for r in rows]


def correction_stats(conn: sqlite3.Connection) -> list[dict]:
    """全ての未教訓化について、届け先・観測が最初に付いた時刻と、前の同じ型の件を返す。

    同じ型の件は、記録役が積むときに前の未教訓化とadd_relationで結んだもの
    （自分より古い未教訓化への関係）で数える。
    """
    rows = conn.execute(
        f"SELECT m.id, m.title, m.created_at FROM materials m WHERE {_IS_CORRECTION} ORDER BY m.id",
        [UNLEARNED_CORRECTION_TAG],
    ).fetchall()
    ids = {r[0] for r in rows}
    result = []
    for mid, title, created_at in rows:
        linked = conn.execute(
            """SELECT r.target_type, r.target_id,
                      COALESCE(l.created_at, m2.created_at) AS at,
                      COALESCE((SELECT group_concat(t.name) FROM log_tags lt JOIN tags t ON t.id = lt.tag_id
                                WHERE lt.log_id = l.id AND t.namespace = ''),
                               (SELECT group_concat(t.name) FROM material_tags mt JOIN tags t ON t.id = mt.tag_id
                                WHERE mt.material_id = m2.id AND t.namespace = '')) AS tag_names
               FROM relations_view r
               LEFT JOIN discussion_logs l
                 ON r.target_type = 'log' AND l.id = r.target_id AND l.retracted_at IS NULL
               LEFT JOIN materials m2
                 ON r.target_type = 'material' AND m2.id = r.target_id AND m2.retracted_at IS NULL
               WHERE r.source_type = 'material' AND r.source_id = ?
                 AND r.target_type IN ('log', 'material')""",
            (mid,),
        ).fetchall()
        delivered_at = observed_at = None
        unobservable = False
        same_type_of = []
        for ttype, tid, at, tag_names in linked:
            names = set((tag_names or "").split(","))
            if at and LESSON_DELIVERY_TAG in names:
                delivered_at = min(filter(None, [delivered_at, at]))
            if at and LESSON_UNOBSERVABLE_TAG in names:
                unobservable = True
            if at and LESSON_OBSERVED_TAG in names:
                observed_at = min(filter(None, [observed_at, at]))
            if ttype == "material" and tid in ids and tid < mid:
                same_type_of.append(tid)
        result.append({
            "id": mid, "title": title, "created_at": created_at,
            "delivered_at": delivered_at, "observed_at": observed_at, "unobservable": unobservable,
            "same_type_of": sorted(same_type_of),
        })
    return result
