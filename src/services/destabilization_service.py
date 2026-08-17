"""destabilizesエッジの解消（resolve）管理・候補提示サービス"""
import logging
import sqlite3

from src.config import PRECEDENT_ROUTING_K_MAX, PRECEDENT_ROUTING_MISS_DISTANCE
from src.db import get_connection
from src.services.precedent_pull_service import route_topics
from src.services.retract_service import retract
from src.services.tag_service import get_effective_tags_batch_by_ids, parse_tag, resolve_tag_ids

logger = logging.getLogger(__name__)

VALID_RESOLUTIONS = {"reaffirmed", "revised", "retracted"}

# suggest_destabilized_candidates のスコア係数。
# ow解体decisionの実例データでのsimulate試験（素案 0.6*embedding_similarity +
# 0.3*tag_jaccard + 0.1*same_topic_bonus と本比較）の結果、tag_jaccard重視版の方が
# 既知の影響decision群の順位が一貫して改善したため採用（cc-memory material
# 「候補提示スコア係数simulate試験結果」参照）。
_SCORE_WEIGHT_EMBEDDING = 0.3
_SCORE_WEIGHT_TAG_JACCARD = 0.6
_SCORE_WEIGHT_SAME_TOPIC = 0.1

# 候補生成のタグ一致条件から除外するタグ。ほぼ全decisionに付与されており、除外しないと
# 候補集合が実質DB全体になり候補生成として機能しない（simulate試験で確認）。
# tag_jaccardのスコア計算そのものではこのタグも含めた有効タグ集合全体を使う
# （候補生成の絞り込みにのみ適用する）。
# cc-memory は CALM に改名され、DB上のタグも domain:cc-memory → domain:calm に
# rename する（旧名 domain:cc-memory は canonical エイリアスとして残す）。
# rename はこのコードのマージ後に実施するため、その間はDB上の実タグ名が旧名のままの
# 期間が存在する。どちらの名前でも除外が効くよう新旧両方を列挙しておく
# （除外が外れると候補集合が実質DB全体に膨らみ機能が壊れる）。
_CANDIDATE_MATCH_EXCLUDED_TAGS = {"domain:calm", "domain:cc-memory"}


def resolve_destabilization(
    source_decision_id: int,
    target_decision_id: int,
    resolution: str,
    revised_to_decision_id: int | None = None,
    note: str = "",
) -> dict:
    """destabilizesエッジ1本を解消（resolve）する。

    decision_destabilization_resolutionsに1行INSERTする（PRIMARY KEY: source_id, target_id）。
    エッジ自体（decision_supersedes側のkind='destabilizes'行）は削除しない（履歴を残す）。

    - resolution="reaffirmed": targetの結論を再確認した（揺らぎ解消、結論変更なし）。
      resolution行をINSERTするのみで、他の副作用はない。
    - resolution="revised": revised_to_decision_idを新結論として記録する。
      supersedesエッジ張り（新decisionがtargetをsupersedeする）は本関数の責務ではなく、
      呼び出し側が別途add_relation(relation_type="supersedes")で行う。
    - resolution="retracted": targetを実際にretractする（decisions.retracted_atを更新）。
      既存のretract_service.retract経路を再利用する。

    同一(source_decision_id, target_decision_id)への2回目以降の呼び出しは、
    PRIMARY KEY制約による重複INSERTを避けるため事前チェックで検出し、
    resolution行を追加せず"already_resolved": trueを返す（冪等）。
    このとき副作用（retracted分岐でのretract呼び出し等）も発生しない。

    Args:
        source_decision_id: 揺らぎの発生元（軸変更）のdecision ID
        target_decision_id: 前提が揺らいだ影響先のdecision ID
        resolution: "reaffirmed" | "revised" | "retracted"
        revised_to_decision_id: resolution="revised"のとき必須。新結論となるdecision ID
        note: 自由記述の注記

    Returns:
        成功時: {"resolved": bool, "already_resolved": bool}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if resolution not in VALID_RESOLUTIONS:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"resolution must be one of: {', '.join(sorted(VALID_RESOLUTIONS))}",
            }
        }
    if resolution == "revised" and revised_to_decision_id is None:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "revised_to_decision_id is required when resolution='revised'",
            }
        }

    conn = get_connection()
    try:
        # 既存resolution行の有無を確認（重複INSERTはPK制約でIntegrityErrorになるため事前チェック）。
        # SELECTのみなのでこの時点ではconnは書き込みトランザクションを開始していない。
        existing = conn.execute(
            "SELECT resolution FROM decision_destabilization_resolutions WHERE source_id = ? AND target_id = ?",
            (source_decision_id, target_decision_id),
        ).fetchone()
        already_resolved = existing is not None

        if not already_resolved and resolution == "retracted":
            # retract_serviceは自前でconn/トランザクションを持ち、内部でcommitまで完結する
            # （本関数のconnとは別コネクション）。connがまだ書き込みトランザクションを
            # 開始していないこのタイミングで先に呼ぶ。conn側のINSERTを先に実行してから
            # 呼ぶと、conn未commitの書き込みロックとretract側の書き込みロックが競合し、
            # busy_timeoutを使い切って"database is locked"で失敗する（実機確認済み）。
            #
            # source_decision_idの存在は後続INSERTのFK制約でしか検証されないが、retract
            # 実行後にFK違反で失敗すると「targetはretractされたがresolution行が残らない」
            # 状態になる。retract発火前に存在チェックし、この不整合を避ける。
            if conn.execute("SELECT 1 FROM decisions WHERE id = ?", (source_decision_id,)).fetchone() is None:
                return {
                    "error": {
                        "code": "CONSTRAINT_VIOLATION",
                        "message": f"source decision {source_decision_id} not found",
                    }
                }
            # ids引数はlist必須（retract(entity_type, ids, undo=False)）。
            retract_result = retract("decision", [target_decision_id])
            if "error" in retract_result:
                return {
                    "error": {
                        "code": "DATABASE_ERROR",
                        "message": f"retract failed: {retract_result['error']['message']}",
                    }
                }
            item_errors = retract_result.get("errors") or []
            if item_errors:
                item_error = item_errors[0]["error"]
                return {
                    "error": {
                        "code": item_error.get("code", "DATABASE_ERROR"),
                        "message": (
                            f"retract failed for decision {target_decision_id}: "
                            f"{item_error.get('message')}"
                        ),
                    }
                }

        if not already_resolved:
            conn.execute(
                "INSERT INTO decision_destabilization_resolutions "
                "(source_id, target_id, resolution, revised_to_decision_id, note) VALUES (?, ?, ?, ?, ?)",
                (source_decision_id, target_decision_id, resolution, revised_to_decision_id, note),
            )

        conn.commit()
        return {"resolved": not already_resolved, "already_resolved": already_resolved}
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"resolve_destabilization failed: {e}")
        return {"error": {"code": "CONSTRAINT_VIOLATION", "message": str(e)}}
    except Exception as e:
        conn.rollback()
        logger.error(f"resolve_destabilization failed: {e}")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


def _get_owner_topic_ids_batch(conn: sqlite3.Connection, decision_ids: list[int]) -> dict[int, "int | None"]:
    """複数decisionの所属topic_idを一括取得する（relations.belongs_to経由）。

    cc-memoryには「decision→所属topic」を直接引く既存関数が無いため、
    relationsテーブルのbelongs_toエッジを直接クエリする。複数topicにbelongs_toする
    decisionは最小のtopic_idを採用する（decision作成時は単一topicが基本で、
    複数belongs_toは後付けのrelated付け時のみ発生するレアケース）。

    Returns: {decision_id: topic_id or None}
    """
    result: dict[int, "int | None"] = {did: None for did in decision_ids}
    if not decision_ids:
        return result
    placeholders = ",".join("?" * len(decision_ids))
    rows = conn.execute(
        f"""
        SELECT source_id AS decision_id, MIN(target_id) AS topic_id
        FROM relations
        WHERE source_type = 'decision' AND source_id IN ({placeholders})
          AND target_type = 'topic' AND relation_type = 'belongs_to'
        GROUP BY source_id
        """,
        tuple(decision_ids),
    ).fetchall()
    for row in rows:
        result[row["decision_id"]] = row["topic_id"]
    return result


def _decisions_in_topics(conn: sqlite3.Connection, topic_ids: list[int], exclude_id: int) -> set[int]:
    """指定topic群にbelongs_toするnon-retract decisionのIDを返す（exclude_idを除く）。"""
    if not topic_ids:
        return set()
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"""
        SELECT d.id FROM decisions d
        JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
        WHERE r.target_id IN ({placeholders}) AND d.retracted_at IS NULL
        """,
        tuple(topic_ids),
    ).fetchall()
    return {row["id"] for row in rows} - {exclude_id}


def _decisions_sharing_tags(conn: sqlite3.Connection, tag_ids: list[int], exclude_id: int) -> set[int]:
    """指定タグIDのいずれかを持つnon-retract decisionのIDを返す（exclude_idを除く）。

    直接付与（decision_tags）と所属topic経由の継承（topic_tags via belongs_to）の両方を見る
    （tag_service.get_effective_tagsと同じ有効タグの考え方）。
    """
    if not tag_ids:
        return set()
    placeholders = ",".join("?" * len(tag_ids))
    rows = conn.execute(
        f"""
        SELECT d.id FROM decisions d
        JOIN decision_tags dt ON dt.decision_id = d.id
        WHERE dt.tag_id IN ({placeholders}) AND d.retracted_at IS NULL

        UNION

        SELECT d.id FROM decisions d
        JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
        JOIN topic_tags tt ON tt.topic_id = r.target_id
        WHERE tt.tag_id IN ({placeholders}) AND d.retracted_at IS NULL
        """,
        tuple(tag_ids) * 2,
    ).fetchall()
    return {row["id"] for row in rows} - {exclude_id}


def _get_decision_titles_batch(conn: sqlite3.Connection, decision_ids: list[int]) -> dict[int, str]:
    """複数decisionの表示用タイトルを一括取得する（titleが無ければdecision本文の先頭50文字）。"""
    if not decision_ids:
        return {}
    placeholders = ",".join("?" * len(decision_ids))
    rows = conn.execute(
        f"SELECT id, title, decision FROM decisions WHERE id IN ({placeholders})",
        tuple(decision_ids),
    ).fetchall()
    return {row["id"]: row["title"] or (row["decision"] or "")[:50] for row in rows}


def _tag_jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def suggest_destabilized_candidates(
    source_decision_id: int,
    k: int = 20,
    include_already_resolved: bool = False,
) -> dict:
    """軸変更decisionからdestabilizeされそうな候補decisionを提示する。

    候補は「(a) sourceとtag集合が重なるnon-retract decision」と「(b) sourceが属するtopicの
    embedding近傍topicに属するnon-retract decision」の和集合。各候補についてtag重なり
    （Jaccard係数）とembedding類似度（route_topicsが返すdistanceを正規化）、および
    同一topicボーナス（same_topic_bonus、重み_SCORE_WEIGHT_SAME_TOPIC）を合成した
    スコア降順で返す。embeddingサーバー停止時は例外にせず、チャネル(b)のみを無効化して
    チャネル(a)（タグ一致）の候補はmode="tag_only"で返し続ける（縮退してもゼロ件には
    しない）。

    read-only。decision_supersedes等への書き込みは一切行わない。実際にdestabilizesエッジを
    張るかどうかは呼び出し側の判断で、別途add_relation(relation_type="destabilizes")を呼ぶ。

    Args:
        source_decision_id: 軸変更decisionのID
        k: 返す候補数の上限（既定20）
        include_already_resolved: Trueのとき、既にresolve_destabilizationで解消済みの
            候補も含める（既定False。解消済みは除外し、同じdecisionを何度も提示しない）

    Returns:
        {"candidates": [{"decision_id", "title", "score", "match_reason",
                          "already_destabilized", "already_resolved"}, ...],
         "mode": "vector" | "tag_only"}
         "vector"はチャネル(a)(b)双方が有効に動作したことを、"tag_only"はembedding
         サーバー停止等でチャネル(b)（embedding近傍）が無効化され、チャネル(a)
         （タグ一致）のみで候補生成したことを示す（候補が0件の場合もこの値になりうる）。
    """
    conn = get_connection()
    try:
        source_tags = set(
            get_effective_tags_batch_by_ids(conn, "decision", [source_decision_id]).get(
                source_decision_id, []
            )
        )

        source_topic_id = _get_owner_topic_ids_batch(conn, [source_decision_id])[source_decision_id]
        neighbor_distance_by_topic: dict[int, float] = {}
        routing_unavailable = False
        if source_topic_id is not None:
            topic_row = conn.execute(
                "SELECT title FROM discussion_topics WHERE id = ?", (source_topic_id,)
            ).fetchone()
            if topic_row is not None:
                # route_topicsのk引数は「selectedにする近傍topic数の上限」を意味する。
                # PRECEDENT_ROUTING_CANDIDATES（KNN探索プール件数、既定10。selected上限
                # より広めに取るための値）を直接渡すのは意味の取り違え。
                # precedent_pull_service.pyの既存呼び出しと同じPRECEDENT_ROUTING_K_MAX
                # （既定5）を、同じmax(1, min(...))のclampパターンで使う。
                routing_k = max(1, min(PRECEDENT_ROUTING_K_MAX, PRECEDENT_ROUTING_K_MAX))
                routing = route_topics(topic_row["title"], routing_k, conn)
                if routing["mode"] == "unavailable":
                    # embeddingチャネル(b)のみ無効化する。タグ一致チャネル(a)は
                    # embeddingに依存しないため、ここで打ち切らず引き続き計算する。
                    routing_unavailable = True
                else:
                    neighbor_distance_by_topic = {
                        c["topic_id"]: c["distance"] for c in routing["candidates"] if c.get("selected")
                    }

        mode = "tag_only" if routing_unavailable else "vector"

        # 候補生成チャネル(a): sourceとタグを共有するnon-retract decision
        matching_tags = source_tags - _CANDIDATE_MATCH_EXCLUDED_TAGS
        matching_tag_ids = resolve_tag_ids(conn, [parse_tag(t) for t in matching_tags])
        tag_sharing_ids = _decisions_sharing_tags(conn, matching_tag_ids, exclude_id=source_decision_id)

        # 候補生成チャネル(b): 近傍topicに属するnon-retract decision
        # （routing_unavailable時はneighbor_distance_by_topicが空のため自然に空集合になる）
        neighbor_ids = _decisions_in_topics(
            conn, list(neighbor_distance_by_topic.keys()), exclude_id=source_decision_id
        )

        candidate_ids = tag_sharing_ids | neighbor_ids
        if not candidate_ids:
            return {"candidates": [], "mode": mode}

        candidate_id_list = list(candidate_ids)
        owner_topic_by_id = _get_owner_topic_ids_batch(conn, candidate_id_list)
        tags_by_id = get_effective_tags_batch_by_ids(conn, "decision", candidate_id_list)
        title_by_id = _get_decision_titles_batch(conn, candidate_id_list)

        already_destabilized_ids = {
            row["target_id"]
            for row in conn.execute(
                "SELECT target_id FROM decision_supersedes WHERE source_id = ? AND kind = 'destabilizes'",
                (source_decision_id,),
            ).fetchall()
        }
        already_resolved_ids = {
            row["target_id"]
            for row in conn.execute(
                "SELECT target_id FROM decision_destabilization_resolutions WHERE source_id = ?",
                (source_decision_id,),
            ).fetchall()
        }

        scored = []
        for did in candidate_id_list:
            if not include_already_resolved and did in already_resolved_ids:
                continue

            cand_tags = set(tags_by_id.get(did, []))
            jaccard = _tag_jaccard(source_tags, cand_tags)

            topic_id = owner_topic_by_id.get(did)
            distance = neighbor_distance_by_topic.get(topic_id) if topic_id is not None else None
            sim = (
                max(0.0, 1.0 - distance / PRECEDENT_ROUTING_MISS_DISTANCE)
                if distance is not None and PRECEDENT_ROUTING_MISS_DISTANCE > 0
                else 0.0
            )
            same_topic_bonus = 1.0 if topic_id is not None and topic_id == source_topic_id else 0.0

            score = (
                _SCORE_WEIGHT_EMBEDDING * sim
                + _SCORE_WEIGHT_TAG_JACCARD * jaccard
                + _SCORE_WEIGHT_SAME_TOPIC * same_topic_bonus
            )

            match_reason = [f"tag_overlap:{t}" for t in sorted(source_tags & cand_tags)]
            if sim > 0:
                match_reason.append(f"embedding_neighbor:{topic_id}")
            if same_topic_bonus:
                match_reason.append("same_topic")

            scored.append({
                "decision_id": did,
                "title": title_by_id.get(did, ""),
                "score": round(score, 4),
                "match_reason": match_reason,
                "already_destabilized": did in already_destabilized_ids,
                "already_resolved": did in already_resolved_ids,
            })

        scored.sort(key=lambda c: (c["score"], c["decision_id"]), reverse=True)
        return {"candidates": scored[:k], "mode": mode}
    finally:
        conn.close()
