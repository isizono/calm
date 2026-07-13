"""判例 pull サービス: topic routing + browse 保証。

設計・裁定の場面で、自由記述の文脈から近傍 topic を特定し（routing）、
その topic に属する非 retract decision を LIMIT なしで網羅列挙する（browse 保証）。
search のようなランク top-N の確率的発見とは異なり、「routing が当たった topic の
decision は全件、最低でも索引粒度で応答に現れる」ことを機構として保証する。

予算超過時も黙って切り捨てず、全件を index 粒度で提示したうえで本文展開数を
予算で制御し、縮退を `truncated` / `budget` で明示する。
"""
import json
import logging
import sqlite3
import threading
from typing import Optional

from sqlite_vec import serialize_float32

from src.config import (
    PRECEDENT_BUDGET_CHARS,
    PRECEDENT_RESPONSE_CHARS_MAX,
    PRECEDENT_ROUTING_CANDIDATES,
    PRECEDENT_ROUTING_K_MAX,
    PRECEDENT_ROUTING_MISS_DISTANCE,
)
from src.db import get_connection, get_db_path, row_to_dict
from src.services.budget_service import allocate_decision_budget, compute_allocation_order
from src.services.embedding_service import encode_query
from src.services.material_service import SNIPPET_MAX_LEN
from src.services.precedent_cluster_service import expand_decision_cluster
from src.services.precedent_pure import parse_precedent_sections
from src.services.readable_id import strip_entity_id_inplace
from src.services.supersede_service import compute_supersede_info_batch, get_superseded_by_batch
from src.services.tag_service import get_effective_tags_batch_by_ids

logger = logging.getLogger(__name__)

_EMPTY_BUDGET_TEMPLATE = {"full": 0, "index_only": 0, "used": 0}


def route_topics(context: str, k: int, conn: sqlite3.Connection) -> dict:
    """topic_vec KNN で近傍 topic 候補を返す。

    embedding サーバー停止時は mode="unavailable" を返す（例外にしない）。
    候補は distance 昇順で並び、distance が PRECEDENT_ROUTING_MISS_DISTANCE 以下の
    ものから先頭 k 件に selected=True が付く。

    Returns:
        {"mode": "vector" | "unavailable",
         "candidates": [{"topic_id", "title", "distance", "selected"}, ...]}
    """
    query_embedding = encode_query(context)
    if query_embedding is None:
        return {"mode": "unavailable", "candidates": []}

    try:
        blob = serialize_float32(query_embedding)
        knn_rows = conn.execute(
            "SELECT rowid, distance FROM topic_vec WHERE embedding MATCH ? AND k = ?",
            (blob, PRECEDENT_ROUTING_CANDIDATES),
        ).fetchall()
    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        # sqlite-vec 拡張未ロード・topic_vec 不整合等での KNN 失敗は routing 不能として
        # 縮退させる（例外にしない）。encode_query の None と同じ unavailable に倒す。
        logger.warning("topic_vec KNN failed, treating routing as unavailable", exc_info=True)
        return {"mode": "unavailable", "candidates": []}
    if not knn_rows:
        return {"mode": "vector", "candidates": []}

    distance_by_id = {row["rowid"]: row["distance"] for row in knn_rows}
    ids = list(distance_by_id.keys())
    placeholders = ",".join("?" * len(ids))
    title_rows = conn.execute(
        f"SELECT id, title FROM discussion_topics WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    title_by_id = {row["id"]: row["title"] for row in title_rows}

    # topic_vec 行の親 topic が既に物理削除されている場合（アプリ層の削除経路が
    # 追いついていないケース）は孤児行として静かに除外する。
    candidates = [
        {"topic_id": tid, "title": title_by_id[tid], "distance": round(distance_by_id[tid], 4)}
        for tid in ids
        if tid in title_by_id
    ]
    candidates.sort(key=lambda c: c["distance"])

    selected_count = 0
    for c in candidates:
        near = c["distance"] <= PRECEDENT_ROUTING_MISS_DISTANCE
        c["selected"] = bool(near and selected_count < k)
        if c["selected"]:
            selected_count += 1

    return {"mode": "vector", "candidates": candidates}


def _explicit_routing_with_conn(conn: sqlite3.Connection, topic_ids: list[int], k: int) -> dict:
    """topic_ids 明示指定時の routing（vector KNN をスキップする）。

    存在しない topic_id は {"topic_id", "error": "not_found"} として候補に残し
    selected 対象から除外する。重複 id は先勝ちで畳む。
    """
    unique_ids = list(dict.fromkeys(topic_ids))
    candidates: list[dict] = []
    if unique_ids:
        placeholders = ",".join("?" * len(unique_ids))
        rows = conn.execute(
            f"SELECT id, title FROM discussion_topics WHERE id IN ({placeholders})",
            tuple(unique_ids),
        ).fetchall()
        title_by_id = {row["id"]: row["title"] for row in rows}
        selected_count = 0
        for tid in unique_ids:
            if tid not in title_by_id:
                candidates.append({"topic_id": tid, "error": "not_found"})
                continue
            selected = selected_count < k
            if selected:
                selected_count += 1
            candidates.append({"topic_id": tid, "title": title_by_id[tid], "selected": selected})
    return {"mode": "explicit", "candidates": candidates}


def _decision_display_title(dec: dict) -> str:
    return dec.get("title") or (dec.get("decision") or "")[:50]


def _build_index_item(
    dec: dict,
    supersede_map: dict[int, dict],
    superseded_by_map: dict[int, Optional[int]],
    material_ids_by_decision: dict[int, set[int]],
    also_in: Optional[list[int]] = None,
) -> dict:
    did = dec["id"]
    info = supersede_map.get(did, {"is_superseded": False})
    item: dict = {
        "id": did,
        "title": _decision_display_title(dec),
        "detail": "index",
        "created_at": dec["created_at"],
        "is_superseded": info["is_superseded"],
        "superseded_by": superseded_by_map.get(did),
    }
    if also_in:
        item["also_in"] = also_in
    mids = material_ids_by_decision.get(did)
    if mids:
        item["material_ids"] = sorted(mids)
    strip_entity_id_inplace(item)
    return item


def _build_full_item(
    dec: dict,
    tags_map: dict[int, list[str]],
    supersede_map: dict[int, dict],
    superseded_by_map: dict[int, Optional[int]],
    material_ids_by_decision: dict[int, set[int]],
) -> dict:
    did = dec["id"]
    info = supersede_map.get(did, {"is_superseded": False, "supersede_chain": [did]})
    item: dict = {
        "id": did,
        "title": _decision_display_title(dec),
        "detail": "full",
        "decision": dec["decision"],
        "reason": dec["reason"],
        "tags": tags_map.get(did, []),
        "created_at": dec["created_at"],
        "is_superseded": info["is_superseded"],
        "superseded_by": superseded_by_map.get(did),
        "supersede_chain": info["supersede_chain"],
    }
    parsed = parse_precedent_sections(dec.get("reason") or "")
    if parsed is not None:
        item["sections"] = parsed
    mids = material_ids_by_decision.get(did)
    if mids:
        item["material_ids"] = sorted(mids)
    strip_entity_id_inplace(item)
    return item


def _collect_material_links(
    conn: sqlite3.Connection,
    all_ids: list[int],
) -> tuple[dict[int, dict], dict[int, set[int]], bool]:
    """all_ids を seed に depth-1 のクラスタ展開を行い、material カタログと
    decision→material リンクを構築する（expand_decision_cluster を利用）。

    expand_decision_cluster は拡張ノード（related/citation で到達した material・
    decision）を既定 30 件で打ち切り、超過分を catalog_overflow に降格する。この経路は
    decision 網羅保証の対象外の補助情報なので超過 material は応答に載せないが、黙って
    落とすと利用側が全 material を見たと誤認するため、超過発生を bool で返して呼出側で
    materials_truncated として明示する。

    Returns: (materials_by_id, material_ids_by_decision, materials_truncated)
    """
    materials_by_id: dict[int, dict] = {}
    material_ids_by_decision: dict[int, set[int]] = {}

    cluster = expand_decision_cluster(conn, all_ids, include_bodies=False)
    materials_truncated = any(
        entry.get("type") == "material" for entry in cluster["catalog_overflow"]
    )
    for entry in cluster["materials"]:
        mid = entry["id_raw"]
        materials_by_id[mid] = {
            "id_raw": mid,
            "title": entry["title"],
            "source": entry["source"],
            "created_at": entry["created_at"],
            "snippet": entry["snippet"],
        }

    all_ids_set = set(all_ids)
    for edge in cluster["edges"]:
        if edge["via"] not in ("related", "citation"):
            continue
        src_type, src_id_s = edge["source"].split(":")
        tgt_type, tgt_id_s = edge["target"].split(":")
        src_id, tgt_id = int(src_id_s), int(tgt_id_s)
        if src_type == "decision" and tgt_type == "material":
            did, mid = src_id, tgt_id
        elif src_type == "material" and tgt_type == "decision":
            mid, did = src_id, tgt_id
        else:
            continue
        if did not in all_ids_set or mid not in materials_by_id:
            continue
        material_ids_by_decision.setdefault(did, set()).add(mid)

    return materials_by_id, material_ids_by_decision, materials_truncated


def _build_topic_materials(
    conn: sqlite3.Connection,
    topic_id: int,
    topic_dec_ids: set[int],
    materials_by_id: dict[int, dict],
    material_ids_by_decision: dict[int, set[int]],
) -> list[dict]:
    """topic 1件分の material カタログを組み立てる。

    (a) この topic の decision と related/citation で結ばれた material（優先。
        linked_decision_ids 付き）と (b) topic に直接 belongs_to する material を
        material_id で合流させる。両方に該当する場合は (a) の linked_decision_ids を残す。
    """
    materials_out: dict[int, dict] = {}

    for did, mids in material_ids_by_decision.items():
        if did not in topic_dec_ids:
            continue
        for mid in mids:
            base = materials_by_id.get(mid)
            if base is None:
                continue
            entry = materials_out.setdefault(mid, {**base, "linked_decision_ids": set()})
            entry["linked_decision_ids"].add(did)

    rows = conn.execute(
        """
        SELECT m.id, m.title, m.source, m.created_at, m.content
        FROM materials m
        JOIN relations r ON r.source_type = 'material' AND r.source_id = m.id
                        AND r.target_type = 'topic' AND r.target_id = ?
                        AND r.relation_type = 'belongs_to'
        WHERE m.retracted_at IS NULL
        """,
        (topic_id,),
    ).fetchall()
    for row in rows:
        mid = row["id"]
        if mid in materials_out:
            continue
        materials_out[mid] = {
            "id_raw": mid,
            "title": row["title"],
            "source": row["source"],
            "created_at": row["created_at"],
            "snippet": (row["content"] or "")[:SNIPPET_MAX_LEN],
            "linked_decision_ids": set(),
        }

    result = list(materials_out.values())
    for entry in result:
        entry["linked_decision_ids"] = sorted(entry["linked_decision_ids"])
    result.sort(key=lambda m: m["id_raw"], reverse=True)
    result.sort(key=lambda m: m["created_at"], reverse=True)
    return result


def _apply_response_size_gate(
    topics_out: list[dict],
    budget: dict,
    truncated: bool,
    materials_truncated: bool,
    full_ids: set[int],
    full_item_locations: dict[int, tuple[int, int]],
    decision_by_id: dict[int, dict],
    supersede_map: dict[int, dict],
    superseded_by_map: dict[int, Optional[int]],
    material_ids_by_decision: dict[int, set[int]],
    response_chars_max: int,
) -> tuple[bool, bool]:
    """本文文字数予算（PRECEDENT_BUDGET_CHARS）内に収まっていても、tags/sections/
    supersede_chain の重複計上やmaterialカタログの併載で実レスポンスが数倍に
    膨れることがある。ここでレスポンス実サイズを最終計測し、超過分を
    full item の降格（配分順の逆順、最後に昇格したものから）で解消する。

    実測対象は topics/budget/truncated/materials_truncated（pull_precedents が
    この後付け足す guarantee/routing は候補数が高々 PRECEDENT_ROUTING_K_MAX 件と
    小さく、実測から除いても上限判定に実質影響しない）。

    降格だけでは解消しない場合は topics[].materials を catalog_overflow 相当
    （type/id_raw/titleのみ）へ縮退する。それでも超過するときはベストエフォートで
    そのまま返す（decisionの網羅保証を壊す削除は行わない）。

    budget dict を in-place で更新し（response_chars キーを追加）、
    (truncated, materials_truncated) の更新後の値を返す。
    """

    def _measure() -> int:
        # ensure_ascii=False で純文字数を測る。MCP送信層がensure_ascii=Trueで
        # 再直列化すると非ASCII文字は\uXXXX表記（1字→6字）に膨張しうるが、
        # response_chars_max の既定値はその差分を安全マージンとして吸収する前提。
        body = {
            "topics": topics_out,
            "budget": budget,
            "truncated": truncated,
            "materials_truncated": materials_truncated,
        }
        return len(json.dumps(body, ensure_ascii=False))

    measured = _measure()
    demoted = 0

    if measured > response_chars_max:
        order = compute_allocation_order(list(full_ids), decision_by_id, supersede_map)
        excess = measured - response_chars_max
        accumulated = 0
        for did in reversed(order):
            location = full_item_locations.get(did)
            if location is None:
                continue
            topic_idx, dec_idx = location
            full_item = topics_out[topic_idx]["decisions"][dec_idx]
            index_item = _build_index_item(
                decision_by_id[did], supersede_map, superseded_by_map, material_ids_by_decision
            )
            cost_full = len(json.dumps(full_item, ensure_ascii=False))
            cost_index = len(json.dumps(index_item, ensure_ascii=False))
            topics_out[topic_idx]["decisions"][dec_idx] = index_item
            demoted += 1
            accumulated += cost_full - cost_index
            if accumulated >= excess:
                break
        measured = _measure()

    if demoted:
        truncated = True

    if measured > response_chars_max:
        for topic in topics_out:
            materials = topic.get("materials")
            if not materials:
                continue
            topic["materials"] = [
                {"type": "material", "id_raw": m["id_raw"], "title": m["title"]} for m in materials
            ]
        materials_truncated = True
        measured = _measure()

    budget["response_chars"] = {
        "limit": response_chars_max,
        # measured はこの代入直前の実測値のため、response_chars フィールド自身
        # （数十字程度）は含まない。
        "measured": measured,
        "demoted": demoted,
    }
    return truncated, materials_truncated


def collect_precedents_with_conn(
    conn: sqlite3.Connection,
    topic_ids: list[int],
    budget_chars: int,
    include_materials: bool,
) -> dict:
    """topic 群の非 retract decision を全件列挙し、予算配分・構造化して返す。

    複数 topic に belongs_to する decision は、選択順で最初に現れた topic 側にのみ
    本文を置く（owner）。他方の topic では detail="index" + also_in 注記になる。
    件数（decisions_total）はこの重複排除より前、topic ごとの実件数で数える。

    Returns:
        {"topics": [...], "budget": {"limit", "used", "full", "index_only", "response_chars"},
         "truncated": bool, "materials_truncated": bool}

    truncated は decision 本文の予算縮退（index 落ち）を、materials_truncated は
    material カタログ展開の 30 件キャップ超過（include_materials 時のみ）を表す。

    budget.limit/used/full/index_only は PRECEDENT_BUDGET_CHARS（decision+reason の
    文字数のみを計上する一次予算）に基づく配分結果であり、この後段のレスポンス
    実サイズゲートによる降格が発生しても再計算しない。budget.response_chars
    （{"limit", "measured", "demoted"}）がレスポンス全体の実測文字数と、その
    ゲートで full から index へ降格した件数を表す。demoted > 0 のときは
    budget.full の一部が実際には detail="index" になっている（実際の full 件数は
    budget.full - demoted）。truncated はこの降格も反映して更新される。
    """
    if not topic_ids:
        return {
            "topics": [],
            "budget": {"limit": budget_chars, **_EMPTY_BUDGET_TEMPLATE},
            "truncated": False,
            "materials_truncated": False,
        }

    topic_titles: dict[int, Optional[str]] = {}
    topic_decisions: dict[int, list[dict]] = {}
    for topic_id in topic_ids:
        row = conn.execute(
            "SELECT title FROM discussion_topics WHERE id = ?", (topic_id,)
        ).fetchone()
        topic_titles[topic_id] = row["title"] if row else None
        dec_rows = conn.execute(
            """
            SELECT d.* FROM decisions d
            JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                            AND r.target_type = 'topic' AND r.target_id = ?
                            AND r.relation_type = 'belongs_to'
            WHERE d.retracted_at IS NULL
            ORDER BY d.created_at ASC, d.id ASC
            """,
            (topic_id,),
        ).fetchall()
        topic_decisions[topic_id] = [row_to_dict(r) for r in dec_rows]

    # owner決定: 複数topicにbelongs_toするdecisionは選択順で最初に現れたtopicのみ本文を持つ
    owner_of: dict[int, int] = {}
    decision_by_id: dict[int, dict] = {}
    for topic_id in topic_ids:
        for dec in topic_decisions[topic_id]:
            decision_by_id.setdefault(dec["id"], dec)
            owner_of.setdefault(dec["id"], topic_id)

    all_ids = list(decision_by_id.keys())

    if not all_ids:
        topics_out = []
        for topic_id in topic_ids:
            entry: dict = {
                "topic_id": topic_id,
                "title": topic_titles[topic_id],
                "decisions_total": 0,
                "decisions": [],
            }
            if include_materials:
                entry["materials"] = []
            strip_entity_id_inplace(entry, id_key="topic_id")
            topics_out.append(entry)
        return {
            "topics": topics_out,
            "budget": {"limit": budget_chars, **_EMPTY_BUDGET_TEMPLATE},
            "truncated": False,
            "materials_truncated": False,
        }

    supersede_map = compute_supersede_info_batch(conn, all_ids)
    superseded_by_map = get_superseded_by_batch(conn, all_ids)
    tags_map = get_effective_tags_batch_by_ids(conn, "decision", all_ids)

    full_ids, used = allocate_decision_budget(all_ids, decision_by_id, supersede_map, budget_chars)

    materials_by_id: dict[int, dict] = {}
    material_ids_by_decision: dict[int, set[int]] = {}
    materials_truncated = False
    if include_materials:
        materials_by_id, material_ids_by_decision, materials_truncated = _collect_material_links(
            conn, all_ids
        )

    topics_out = []
    full_item_locations: dict[int, tuple[int, int]] = {}
    for topic_id in topic_ids:
        topic_dec_ids = {dec["id"] for dec in topic_decisions[topic_id]}
        decisions_out = []
        for dec in topic_decisions[topic_id]:
            did = dec["id"]
            owner = owner_of[did]
            if owner != topic_id:
                item = _build_index_item(
                    dec, supersede_map, superseded_by_map, material_ids_by_decision, also_in=[owner]
                )
            elif did in full_ids:
                item = _build_full_item(
                    dec, tags_map, supersede_map, superseded_by_map, material_ids_by_decision
                )
                full_item_locations[did] = (len(topics_out), len(decisions_out))
            else:
                item = _build_index_item(dec, supersede_map, superseded_by_map, material_ids_by_decision)
            decisions_out.append(item)

        topic_entry: dict = {
            "topic_id": topic_id,
            "title": topic_titles[topic_id],
            "decisions_total": len(topic_decisions[topic_id]),
            "decisions": decisions_out,
        }
        if include_materials:
            topic_entry["materials"] = _build_topic_materials(
                conn, topic_id, topic_dec_ids, materials_by_id, material_ids_by_decision
            )
        strip_entity_id_inplace(topic_entry, id_key="topic_id")
        topics_out.append(topic_entry)

    index_only = len(all_ids) - len(full_ids)
    budget = {"limit": budget_chars, "used": used, "full": len(full_ids), "index_only": index_only}
    truncated = index_only > 0
    truncated, materials_truncated = _apply_response_size_gate(
        topics_out,
        budget,
        truncated,
        materials_truncated,
        full_ids,
        full_item_locations,
        decision_by_id,
        supersede_map,
        superseded_by_map,
        material_ids_by_decision,
        PRECEDENT_RESPONSE_CHARS_MAX,
    )
    return {
        "topics": topics_out,
        "budget": budget,
        "truncated": truncated,
        "materials_truncated": materials_truncated,
    }


def pull_precedents(
    context: str,
    topic_ids: Optional[list[int]] = None,
    k: int = 3,
    budget_chars: Optional[int] = None,
    include_materials: bool = True,
) -> dict:
    """route_topics + collect_precedents_with_conn の合成。MCP ツール本体（flavor 適用は呼出側）。

    Args:
        context: routing のクエリになる自由記述の文脈（2文字以上必須）。
                 topic_ids 指定時も telemetry 用に必須
        topic_ids: 指定時は routing をスキップし、対象 topic を明示する
        k: routing で採用する topic 数の上限（1..PRECEDENT_ROUTING_K_MAX にclamp）
        budget_chars: 本文展開の文字数予算（省略時 PRECEDENT_BUDGET_CHARS）
        include_materials: decision に紐づく material と topic 直下 material を同時展開する

    Returns:
        {"guarantee", "routing", "topics", "budget", "truncated", "materials_truncated"}。
        guarantee は "enumerated" / "routing_miss" / "routing_unavailable"。
        materials_truncated は material カタログ展開が 30 件キャップを超えて一部 material を
        載せ切れなかったこと、またはレスポンス実サイズゲート（下記）による material
        カタログ縮退が発生したことを表す（include_materials 時のみ true になり得る）。

        budget_chars は decision+reason 本文のみを計上する一次予算であり、実際の
        レスポンスには tags/sections/supersede_chain/material カタログ等も乗るため、
        この一次予算内でも応答全体が PRECEDENT_RESPONSE_CHARS_MAX を超えることがある。
        超過時は full item を配分順の逆順で index item へ降格して解消し（それでも
        超過するときは material カタログを縮退）、budget.response_chars に
        {"limit", "measured", "demoted"} として結果を記録する。budget.full/index_only
        はこの降格を反映せず一次予算の配分結果のまま（詳細は collect_precedents_with_conn
        docstring参照）。
    """
    if not context or len(context.strip()) < 2:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "context must be at least 2 characters",
            }
        }

    k = max(1, min(k, PRECEDENT_ROUTING_K_MAX))
    budget = budget_chars if budget_chars is not None else PRECEDENT_BUDGET_CHARS

    conn = get_connection()
    try:
        if topic_ids is not None:
            routing = _explicit_routing_with_conn(conn, topic_ids, k)
        else:
            routing = route_topics(context, k, conn)

        for candidate in routing["candidates"]:
            strip_entity_id_inplace(candidate, id_key="topic_id")

        selected_ids = [c["topic_id_raw"] for c in routing["candidates"] if c.get("selected")]

        if routing["mode"] == "unavailable":
            guarantee = "routing_unavailable"
            collected = {
                "topics": [],
                "budget": {"limit": budget, **_EMPTY_BUDGET_TEMPLATE},
                "truncated": False,
                "materials_truncated": False,
            }
        elif not selected_ids:
            guarantee = "routing_miss"
            collected = {
                "topics": [],
                "budget": {"limit": budget, **_EMPTY_BUDGET_TEMPLATE},
                "truncated": False,
                "materials_truncated": False,
            }
        else:
            guarantee = "enumerated"
            collected = collect_precedents_with_conn(conn, selected_ids, budget, include_materials)

        result = {
            "guarantee": guarantee,
            "routing": {"mode": routing["mode"], "candidates": routing["candidates"]},
            **collected,
        }

        decisions_total = sum(t["decisions_total"] for t in collected["topics"])
        _record_precedent_telemetry_async(
            context,
            {
                "topic_ids": topic_ids,
                "k": k,
                "budget_chars": budget,
                "include_materials": include_materials,
            },
            guarantee,
            routing,
            decisions_total,
            collected["budget"]["full"],
        )
        return result
    finally:
        conn.close()


# ========================================
# telemetry（非同期書込。search_telemetry と同型のパターンを踏襲）
# ========================================


def _telemetry_get_connection() -> sqlite3.Connection:
    """telemetry 書込専用の軽量コネクション（sqlite-vec 拡張ロードを省く）。"""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _record_precedent_telemetry_async(
    context: str,
    parameters: dict,
    guarantee: str,
    routing: dict,
    decisions_total: int,
    full_count: int,
) -> Optional[threading.Thread]:
    """pull_precedents 呼出の telemetry を別スレッドで非同期書込する。

    書込失敗（シリアライズ・DB・スレッド起動のいずれも）は呼出元の応答を壊さず
    logger.warning に握りつぶす。
    """

    def _write() -> None:
        try:
            parameters_json = json.dumps(parameters, ensure_ascii=False)
            routing_json = json.dumps(routing, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.warning("precedent_telemetry serialize failed: %s", e)
            return

        try:
            conn = _telemetry_get_connection()
            try:
                conn.execute(
                    "INSERT INTO precedent_telemetry "
                    "(context, parameters, guarantee, routing_json, decisions_total, full_count) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (context, parameters_json, guarantee, routing_json, int(decisions_total), int(full_count)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("precedent_telemetry write failed: %s", e)

    try:
        thread = threading.Thread(target=_write, daemon=True)
        thread.start()
    except Exception as e:
        logger.warning("precedent_telemetry thread start failed: %s", e)
        return None
    return thread
