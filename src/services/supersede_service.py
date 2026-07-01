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


def compute_supersede_info_batch(
    conn: sqlite3.Connection,
    decision_ids: list[int],
) -> dict[int, dict]:
    """複数 decision に対して supersede chain / is_superseded を一括算出する。

    Returns:
        {decision_id: {"is_superseded": bool, "supersede_chain": [ids...]}}
    """
    if not decision_ids:
        return {}
    return {did: compute_supersede_info(conn, did) for did in decision_ids}


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
        f"WHERE target_id IN ({placeholders}) "
        f"ORDER BY target_id, created_at DESC, source_id DESC",
        tuple(decision_ids),
    ).fetchall()
    for r in rows:
        tid = r["target_id"]
        if result[tid] is None:
            result[tid] = r["source_id"]
    return result
