"""判例クラスタ展開サービス（DB 依存側）。

browse 注入コンポーネント（本設計の非スコープ）が topic 列挙で得た decision 集合を
seed に、(i) supersede 系譜、(ii) depth-1 の related エッジ、(iii) depth-1 の citation
エッジを連結クラスタとして 1 回の呼び出しで返す。語彙が異なる論理的関連（系譜・裏付け）
は類似度検索では拾えないため、本サービスのグラフ走査が補完する。

エッジ源は decision_supersedes（全閉包）/ relations（related, depth 1）/ citations
（順方向・逆方向、depth 1）の 3 種。`get_map` は decision をカタログから落とし
citations を辿らないため代替にならない。
"""
import sqlite3

from src.db import row_to_dict
from src.services.decision_service import _build_decision_item
from src.services.material_service import SNIPPET_MAX_LEN
from src.services.readable_id import apply_readable_id_inplace
from src.services.supersede_service import compute_supersede_info_batch, get_superseded_by_batch
from src.services.tag_service import get_effective_tags_batch_by_ids, get_entity_tags_batch

DEFAULT_MAX_EXPANSION_NODES = 30

# membership 出力順序（複数該当時にこの順で並べる）
_MEMBERSHIP_ORDER = ("seed", "supersede", "related", "cited")

NodeKey = tuple[str, int]


def _decision_display_title(row: dict) -> str:
    return row.get("title") or (row.get("decision") or "")[:50]


def _empty_result() -> dict:
    return {
        "decisions": [],
        "materials": [],
        "edges": [],
        "catalog_overflow": [],
        "excluded_retracted": 0,
        "truncated": False,
    }


def expand_decision_cluster(
    conn: sqlite3.Connection,
    seed_decision_ids: list[int],
    *,
    max_expansion_nodes: int = DEFAULT_MAX_EXPANSION_NODES,
    include_bodies: bool = True,
) -> dict:
    """seed decision 群から判例クラスタを展開する。

    アルゴリズム:
        1. supersede 閉包: `compute_supersede_info_batch` で各 seed の chain を取り、
           chain 上の全 decision id を集合 S に加える。topic 境界を越え得る。
           retract 済みメンバーも除外せず is_retracted=true で結果に含める。
        2. depth-1 拡張: S を起点に related（decision↔decision, decision↔material）
           と citations（順方向・逆方向）を 1 クエリずつ収集する。
        3. retract フィルタ（拡張ノードのみ）: 拡張で到達した decision/material の
           うち retract 済みは結果から除外し `excluded_retracted` に計数する
           （S のメンバーは対象外、常に含める）。
        4. 予算適用: S は常に全件返す。拡張ノードが `max_expansion_nodes` を超えたら
           超過分を `catalog_overflow`（id+title のみ）に降格し `truncated=true` を
           立てる。並び順は (membership 優先度: cited > related, created_at 降順, id
           昇順) の決定的順序。
        5. ペイロード取得・6. edges 構築。

    Args:
        conn: DB コネクション
        seed_decision_ids: 起点となる decision id のリスト
        max_expansion_nodes: supersede 閉包を除く拡張ノードの予算（既定 30）
        include_bodies: decision/reason 本文を結果に含めるか

    Returns:
        {
          "decisions": [{
              "id_raw": int, "title": str, "created_at": str,
              "decision": str, "reason": str,          # include_bodies=True のとき
              "is_retracted": bool, "is_superseded": bool,
              "superseded_by": int | None,
              "supersede_chain": [int, ...],
              "precedent": {...} | 省略,
              "membership": ["seed" | "supersede" | "related" | "cited", ...],
          }, ...],
          "materials": [{
              "id_raw": int, "title": str, "source": str, "created_at": str,
              "snippet": str, "tags": [str, ...], "membership": [...],
          }, ...],
          "edges": [{"source": "decision:12", "target": "decision:8", "via": "supersedes"}, ...],
          "catalog_overflow": [{"type": str, "id_raw": int, "title": str}, ...],
          "excluded_retracted": int,
          "truncated": bool,
        }
    """
    if not seed_decision_ids:
        return _empty_result()

    seed_ids = list(dict.fromkeys(seed_decision_ids))
    seed_set = set(seed_ids)

    # --- 1. supersede 閉包 ---
    seed_supersede_map = compute_supersede_info_batch(conn, seed_ids)
    closure_ids: set[int] = set(seed_set)
    for did in seed_ids:
        info = seed_supersede_map.get(did)
        if info:
            closure_ids.update(info["supersede_chain"])

    membership: dict[NodeKey, set[str]] = {}
    for did in closure_ids:
        key: NodeKey = ("decision", did)
        membership.setdefault(key, set()).add("seed" if did in seed_set else "supersede")

    edges_raw: list[tuple[NodeKey, NodeKey, str]] = []
    edges_seen: set = set()

    def _add_edge(source_key: NodeKey, target_key: NodeKey, via: str) -> None:
        # related は対称関係のため無向ペアで重複排除する。supersedes/citation は
        # 方向に意味があるため有向ペアのまま重複排除する。
        dedup_key = (via, frozenset((source_key, target_key))) if via == "related" else (via, source_key, target_key)
        if dedup_key in edges_seen:
            return
        edges_seen.add(dedup_key)
        edges_raw.append((source_key, target_key, via))

    # supersede edges（closure 内で完結するペアのみ）
    if closure_ids:
        placeholders = ",".join("?" * len(closure_ids))
        rows = conn.execute(
            f"SELECT source_id, target_id FROM decision_supersedes "
            f"WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders})",
            tuple(closure_ids) + tuple(closure_ids),
        ).fetchall()
        for r in rows:
            _add_edge(("decision", r["source_id"]), ("decision", r["target_id"]), "supersedes")

    # --- 2. depth-1 拡張: related + citations（順方向・逆方向） ---
    expansion_touch: dict[NodeKey, set[str]] = {}

    def _touch(node_key: NodeKey, membership_via: str, source_key: NodeKey, target_key: NodeKey, edge_via: str) -> None:
        _add_edge(source_key, target_key, edge_via)
        if node_key in membership:
            # 既に supersede 閉包メンバー: 拡張ノードとしては扱わず membership に追加するのみ
            membership[node_key].add(membership_via)
        else:
            expansion_touch.setdefault(node_key, set()).add(membership_via)

    if closure_ids:
        placeholders = ",".join("?" * len(closure_ids))

        # related（relations_view は双方向展開済みなので source 側の走査だけで depth-1 が拾える）
        for row in conn.execute(
            f"""
            SELECT source_id, target_type, target_id
            FROM relations_view
            WHERE source_type = 'decision' AND source_id IN ({placeholders})
              AND target_type IN ('decision', 'material')
              AND relation_type = 'related'
            """,
            tuple(closure_ids),
        ).fetchall():
            node_key: NodeKey = (row["target_type"], row["target_id"])
            _touch(node_key, "related", ("decision", row["source_id"]), node_key, "related")

        # citation 順方向: closure の decision が cite する先
        for row in conn.execute(
            f"""
            SELECT DISTINCT owner_id, target_type, target_id
            FROM citations
            WHERE owner_type = 'decision' AND owner_id IN ({placeholders})
              AND target_type IN ('decision', 'material')
            """,
            tuple(closure_ids),
        ).fetchall():
            node_key = (row["target_type"], row["target_id"])
            _touch(node_key, "cited", ("decision", row["owner_id"]), node_key, "citation")

        # citation 逆方向: closure の decision を cite している側
        for row in conn.execute(
            f"""
            SELECT DISTINCT owner_type, owner_id, target_id
            FROM citations
            WHERE target_type = 'decision' AND target_id IN ({placeholders})
              AND owner_type IN ('decision', 'material')
            """,
            tuple(closure_ids),
        ).fetchall():
            node_key = (row["owner_type"], row["owner_id"])
            _touch(node_key, "cited", node_key, ("decision", row["target_id"]), "citation")

    # --- 3. retract フィルタ（拡張ノードのみ） ---
    decision_candidate_ids = [nid for (ntype, nid) in expansion_touch if ntype == "decision"]
    material_candidate_ids = [nid for (ntype, nid) in expansion_touch if ntype == "material"]

    expansion_decision_rows: dict[int, dict] = {}
    if decision_candidate_ids:
        placeholders = ",".join("?" * len(decision_candidate_ids))
        for row in conn.execute(
            f"SELECT * FROM decisions WHERE id IN ({placeholders})", tuple(decision_candidate_ids)
        ).fetchall():
            expansion_decision_rows[row["id"]] = row_to_dict(row)

    expansion_material_rows: dict[int, dict] = {}
    if material_candidate_ids:
        placeholders = ",".join("?" * len(material_candidate_ids))
        for row in conn.execute(
            f"SELECT * FROM materials WHERE id IN ({placeholders})", tuple(material_candidate_ids)
        ).fetchall():
            expansion_material_rows[row["id"]] = row_to_dict(row)

    excluded_retracted = 0
    live_candidates: list[NodeKey] = []
    for node_key in expansion_touch:
        ntype, nid = node_key
        row = expansion_decision_rows.get(nid) if ntype == "decision" else expansion_material_rows.get(nid)
        if row is None:
            # 参照先が既に物理削除されている等: 静かに落とす（excluded_retracted には数えない）
            continue
        if row.get("retracted_at"):
            excluded_retracted += 1
            continue
        live_candidates.append(node_key)

    # --- 4. 予算適用 ---
    def _row_for(node_key: NodeKey) -> dict:
        ntype, nid = node_key
        return expansion_decision_rows[nid] if ntype == "decision" else expansion_material_rows[nid]

    # 決定的ソート: id 昇順 → created_at 降順 → membership 優先度（cited > related）
    # sorted() の安定性を利用し、最下位キーから順に適用する。
    live_candidates.sort(key=lambda k: k[1])
    live_candidates.sort(key=lambda k: _row_for(k)["created_at"], reverse=True)
    live_candidates.sort(key=lambda k: 0 if "cited" in expansion_touch[k] else 1)

    truncated = len(live_candidates) > max_expansion_nodes
    included_candidates = live_candidates[:max_expansion_nodes]
    overflow_candidates = live_candidates[max_expansion_nodes:]

    for node_key in included_candidates:
        membership[node_key] = expansion_touch[node_key]

    catalog_overflow: list[dict] = []
    for node_key in overflow_candidates:
        ntype, nid = node_key
        row = _row_for(node_key)
        title = _decision_display_title(row) if ntype == "decision" else row["title"]
        entry = {"type": ntype, "id": nid, "title": title}
        apply_readable_id_inplace(entry, ntype)
        catalog_overflow.append(entry)

    included_node_keys = set(membership.keys())

    # --- 5. ペイロード取得 ---
    decision_ids_final = sorted(nid for (ntype, nid) in included_node_keys if ntype == "decision")
    material_ids_final = sorted(nid for (ntype, nid) in included_node_keys if ntype == "material")

    decision_rows: dict[int, dict] = dict(expansion_decision_rows)
    missing_decision_ids = [did for did in decision_ids_final if did not in decision_rows]
    if missing_decision_ids:
        placeholders = ",".join("?" * len(missing_decision_ids))
        for row in conn.execute(
            f"SELECT * FROM decisions WHERE id IN ({placeholders})", tuple(missing_decision_ids)
        ).fetchall():
            decision_rows[row["id"]] = row_to_dict(row)

    material_rows: dict[int, dict] = dict(expansion_material_rows)

    tags_map = get_effective_tags_batch_by_ids(conn, "decision", decision_ids_final) if decision_ids_final else {}
    # supersede 情報は decision id 単位で決まり batch 構成に依存しない。seed 分は closure
    # 算出で既に得ているため再利用し、未算出の id（chain メンバー・拡張 decision）だけ追加取得する。
    supersede_map = {did: seed_supersede_map[did] for did in decision_ids_final if did in seed_supersede_map}
    missing_supersede_ids = [did for did in decision_ids_final if did not in supersede_map]
    if missing_supersede_ids:
        supersede_map.update(compute_supersede_info_batch(conn, missing_supersede_ids))
    superseded_by_map = get_superseded_by_batch(conn, decision_ids_final)

    decisions_out: list[dict] = []
    for did in decision_ids_final:
        row = decision_rows.get(did)
        if row is None:
            # seed/chain に存在するが decisions テーブルに実体が無い（削除済み等）: 落とす
            continue
        item = _build_decision_item(row, tags_map, supersede_map)
        item["superseded_by"] = superseded_by_map.get(did)
        item["membership"] = [m for m in _MEMBERSHIP_ORDER if m in membership[("decision", did)]]
        if not include_bodies:
            item.pop("decision", None)
            item.pop("reason", None)
        decisions_out.append(item)
    decisions_out.sort(key=lambda i: (i["created_at"], i["id_raw"]))

    material_tags_map = (
        get_entity_tags_batch(conn, "material_tags", "material_id", material_ids_final)
        if material_ids_final
        else {}
    )
    materials_out: list[dict] = []
    for mid in material_ids_final:
        row = material_rows.get(mid)
        if row is None:
            continue
        item = {
            "id": row["id"],
            "title": row["title"],
            "source": row["source"],
            "created_at": row["created_at"],
            "snippet": (row["content"] or "")[:SNIPPET_MAX_LEN],
            "tags": material_tags_map.get(mid, []),
            "membership": [m for m in _MEMBERSHIP_ORDER if m in membership[("material", mid)]],
        }
        apply_readable_id_inplace(item, "material")
        materials_out.append(item)
    materials_out.sort(key=lambda i: (i["created_at"], i["id_raw"]))

    # --- 6. edges 構築（採用されたノードのみを対象にする） ---
    edges_out: list[dict] = []
    for source_key, target_key, via in edges_raw:
        if source_key in included_node_keys and target_key in included_node_keys:
            edges_out.append(
                {
                    "source": f"{source_key[0]}:{source_key[1]}",
                    "target": f"{target_key[0]}:{target_key[1]}",
                    "via": via,
                }
            )
    edges_out.sort(key=lambda e: (e["source"], e["target"], e["via"]))

    return {
        "decisions": decisions_out,
        "materials": materials_out,
        "edges": edges_out,
        "catalog_overflow": catalog_overflow,
        "excluded_retracted": excluded_retracted,
        "truncated": truncated,
    }
