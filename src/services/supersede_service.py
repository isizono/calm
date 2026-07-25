"""decision_supersedes を辿って chain / superseded_by を算出するヘルパー。

decision_supersedes(source_id, target_id): source が target を supersede する。
source が新しい / target が古い。多対多。
"""
import sqlite3
from typing import Optional


def _bfs_related(
    conn: sqlite3.Connection,
    start_id: int,
    direction: str,
) -> set[int]:
    """direction 方向に BFS で decision_supersedes を辿り、到達可能な decision_id を返す。

    Args:
        direction: "older" なら source_id=x → target_id を辿る (古い方向)、
                   "newer" なら target_id=x → source_id を辿る (新しい方向)。

    Returns:
        start_id を含まない、到達可能な decision_id の集合。
    """
    if direction == "older":
        query = "SELECT target_id AS next_id FROM decision_supersedes WHERE source_id = ?"
    elif direction == "newer":
        query = "SELECT source_id AS next_id FROM decision_supersedes WHERE target_id = ?"
    else:
        raise ValueError(f"invalid direction: {direction}")

    reached: set[int] = set()
    visited: set[int] = {start_id}
    queue: list[int] = [start_id]
    while queue:
        current = queue.pop(0)
        rows = conn.execute(query, (current,)).fetchall()
        for r in rows:
            nid = r["next_id"]
            if nid not in visited:
                visited.add(nid)
                reached.add(nid)
                queue.append(nid)
    return reached


def compute_supersede_info(
    conn: sqlite3.Connection,
    decision_id: int,
) -> dict:
    """単一 decision の supersede chain / is_superseded を算出する。

    supersede_chain は decision_id 自身 + 古い方向で到達できる decision + 新しい方向で
    到達できる decision の全体を、created_at 昇順 (同時刻は id 昇順) で並べた id 配列。
    未 supersede でかつ何も supersede していない場合は [decision_id] のみを返す。

    is_superseded は「新しい方向」に少なくとも1件到達可能かで判定する。retract の
    有無は分岐しない (retract 済み decision も chain には含める)。
    """
    older = _bfs_related(conn, decision_id, direction="older")
    newer = _bfs_related(conn, decision_id, direction="newer")

    is_superseded = bool(newer)

    all_ids = older | newer | {decision_id}
    if len(all_ids) == 1:
        return {
            "is_superseded": is_superseded,
            "supersede_chain": [decision_id],
        }

    placeholders = ",".join("?" * len(all_ids))
    rows = conn.execute(
        f"SELECT id FROM decisions WHERE id IN ({placeholders}) "
        f"ORDER BY created_at ASC, id ASC",
        tuple(all_ids),
    ).fetchall()
    chain = [r["id"] for r in rows]
    return {
        "is_superseded": is_superseded,
        "supersede_chain": chain,
    }


def _reach_in_memory(start_id: int, adjacency: dict[int, list[int]]) -> set[int]:
    """メモリ上の隣接リストを BFS で辿り、start_id から到達可能な id を返す (start 自身は含まない)。"""
    reached: set[int] = set()
    visited: set[int] = {start_id}
    queue: list[int] = [start_id]
    while queue:
        current = queue.pop(0)
        for nid in adjacency.get(current, ()):
            if nid not in visited:
                visited.add(nid)
                reached.add(nid)
                queue.append(nid)
    return reached


def compute_supersede_info_batch(
    conn: sqlite3.Connection,
    decision_ids: list[int],
) -> dict[int, dict]:
    """複数 decision に対して supersede chain / is_superseded を一括算出する。

    decision_supersedes を全件1クエリで読み、方向別の隣接リストをメモリ上に構築してから
    各 decision の到達可能集合を辿る。supersede 関係は疎なため全件読みでも軽量で、decision
    数に比例した追加クエリ (N+1) を避けられる。chain 内 id の created_at も1クエリで一括取得
    してソートに使う。

    Returns:
        {decision_id: {"is_superseded": bool, "supersede_chain": [ids...]}}
    """
    if not decision_ids:
        return {}

    # source が target を supersede する。older 方向は source→target、newer 方向は target→source。
    # kind='replaces' のみを辿る（kind='destabilizes' は別関数 compute_destabilization_info_batch の管轄）。
    older_adj: dict[int, list[int]] = {}
    newer_adj: dict[int, list[int]] = {}
    for r in conn.execute(
        "SELECT source_id, target_id FROM decision_supersedes WHERE kind = 'replaces'"
    ).fetchall():
        s, t = r["source_id"], r["target_id"]
        older_adj.setdefault(s, []).append(t)
        newer_adj.setdefault(t, []).append(s)

    chain_ids_by_decision: dict[int, set[int]] = {}
    is_superseded_by_decision: dict[int, bool] = {}
    all_chain_ids: set[int] = set()
    for did in decision_ids:
        older = _reach_in_memory(did, older_adj)
        newer = _reach_in_memory(did, newer_adj)
        chain_ids = older | newer | {did}
        chain_ids_by_decision[did] = chain_ids
        is_superseded_by_decision[did] = bool(newer)
        all_chain_ids |= chain_ids

    # chain を created_at 昇順 (同時刻は id 昇順) に並べるためのソートキーを一括取得する
    order_key: dict[int, tuple] = {}
    placeholders = ",".join("?" * len(all_chain_ids))
    for r in conn.execute(
        f"SELECT id, created_at FROM decisions WHERE id IN ({placeholders})",
        tuple(all_chain_ids),
    ).fetchall():
        order_key[r["id"]] = (r["created_at"], r["id"])

    result: dict[int, dict] = {}
    for did in decision_ids:
        chain = sorted(
            (i for i in chain_ids_by_decision[did] if i in order_key),
            key=lambda i: order_key[i],
        )
        result[did] = {
            "is_superseded": is_superseded_by_decision[did],
            "supersede_chain": chain,
        }
    return result


def compute_destabilization_info_batch(
    conn: sqlite3.Connection, decision_ids: list[int]
) -> dict[int, dict]:
    """複数 decision に対して未resolveな destabilization 情報を一括算出する。

    decision_supersedes の kind='destabilizes' エッジのうち、
    decision_destabilization_resolutions に該当行が無い（未resolve）ものだけを target
    decision 単位で集計する。destabilizes エッジが1本も無い、または全て resolve 済みの
    decision は返り値dictにキー自体を含めない（呼び出し側は `if did in result` で判定する）。

    Returns:
        {decision_id: {
            "destabilized_by": [source_id, ...],       # created_at 昇順
            "unresolved_count": int,
            "latest_source": source_id | None,          # created_at が最も新しい source
            "sources": [{"decision_id", "title", "created_at", "kind_reason"}, ...],
        }}
    """
    if not decision_ids:
        return {}

    placeholders = ",".join("?" * len(decision_ids))
    edge_rows = conn.execute(
        f"SELECT source_id, target_id, created_at FROM decision_supersedes "
        f"WHERE kind = 'destabilizes' AND target_id IN ({placeholders})",
        tuple(decision_ids),
    ).fetchall()
    if not edge_rows:
        return {}

    resolved_pairs = {
        (r["source_id"], r["target_id"])
        for r in conn.execute(
            "SELECT source_id, target_id FROM decision_destabilization_resolutions "
            f"WHERE target_id IN ({placeholders})",
            tuple(decision_ids),
        ).fetchall()
    }

    unresolved_by_target: dict[int, list[tuple[int, str]]] = {}
    for r in edge_rows:
        if (r["source_id"], r["target_id"]) in resolved_pairs:
            continue
        unresolved_by_target.setdefault(r["target_id"], []).append(
            (r["source_id"], r["created_at"])
        )

    if not unresolved_by_target:
        return {}

    source_ids = {sid for pairs in unresolved_by_target.values() for sid, _ in pairs}
    title_placeholders = ",".join("?" * len(source_ids))
    source_rows = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, title, decision, reason FROM decisions WHERE id IN ({title_placeholders})",
            tuple(source_ids),
        ).fetchall()
    }
    titles = {
        sid: r["title"] or (r["decision"] or "")[:50]
        for sid, r in source_rows.items()
    }
    kind_reasons = {
        sid: (r["reason"] or "")[:200]
        for sid, r in source_rows.items()
    }

    result: dict[int, dict] = {}
    for did, pairs in unresolved_by_target.items():
        pairs_sorted = sorted(pairs, key=lambda p: p[1])  # created_at昇順
        latest = pairs_sorted[-1][0] if pairs_sorted else None
        result[did] = {
            "destabilized_by": [sid for sid, _ in pairs_sorted],
            "unresolved_count": len(pairs_sorted),
            "latest_source": latest,
            "sources": [
                {
                    "decision_id": sid,
                    "title": titles.get(sid, ""),
                    "created_at": ca,
                    "kind_reason": kind_reasons.get(sid, ""),
                }
                for sid, ca in pairs_sorted
            ],
        }
    return result


def get_superseded_by_batch(
    conn: sqlite3.Connection,
    decision_ids: list[int],
) -> dict[int, Optional[int]]:
    """各 decision_id について、それを supersede している最新の source_id を返す。

    複数 superseder が存在する場合は decision_supersedes.created_at が最新の1件を採用する
    (pin_service._is_decision_superseded と同じ規則)。superseder が無ければ None。

    Returns:
        {decision_id: latest_superseder_id or None}
    """
    result: dict[int, Optional[int]] = {did: None for did in decision_ids}
    if not decision_ids:
        return result

    placeholders = ",".join("?" * len(decision_ids))
    rows = conn.execute(
        f"SELECT target_id, source_id FROM decision_supersedes "
        f"WHERE target_id IN ({placeholders}) AND kind = 'replaces' "
        f"ORDER BY target_id, created_at DESC, source_id DESC",
        tuple(decision_ids),
    ).fetchall()
    for r in rows:
        tid = r["target_id"]
        if result[tid] is None:
            result[tid] = r["source_id"]
    return result
