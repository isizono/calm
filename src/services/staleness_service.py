"""判例（decision）の陳腐化管理: supersede chain の推移的な head 算出と鮮度メタデータの付与。

方針は自動失効ではなく、鮮度メタデータ（supersede 状態・chain head・経過日数・検証アンカー）
を読み出し面に併記し、裁定は読んだセッションに委ねること。アンカーの意味判定（commit が現
HEAD の祖先か等）はサーバの知識外のため行わない。

検証アンカーの文法・パーサは precedent_pure（文法の正本は docs/precedent-format.md）が唯一の
実装であり、本モジュールは独自の正規表現を持たずそれを import して使う。
"""
import sqlite3
from datetime import datetime, timezone

from src.services.precedent_pure import parse_precedent_sections
from src.services.supersede_service import get_superseded_by_batch


def _extract_anchors(text: str) -> list[dict]:
    """precedent_pure のパース結果から verification_anchors のみ取り出す薄いヘルパー。

    Args:
        text: decision の reason 等、定型節を含みうる本文。

    Returns:
        [{"raw": str, "date": str | None, "commit": str | None}, ...]。
        定型節が無い、または検証アンカーが無ければ空リストを返す。
    """
    parsed = parse_precedent_sections(text or "")
    if parsed is None:
        return []
    return list(parsed.get("verification_anchors") or [])


def get_chain_heads_batch(
    conn: sqlite3.Connection, decision_ids: list[int]
) -> dict[int, list[int]]:
    """各 decision について supersede chain の推移的な最新端（head）を返す。

    decision_supersedes を全件1クエリで読み（compute_supersede_info_batch と同じ全表読み
    方式）、各 decision から新しい方向（それを supersede している decision）に到達できる
    集合のうち、「それ自体をさらに supersede するものが無い」id を head として返す。DAG
    なので head は複数になりうる。

    自身が誰にも supersede されていなければ head は [自身の id] のみ。retract 済み
    decision も chain 表示と同じ扱いで head 候補に含める（意味判定はしない）。

    Returns:
        {decision_id: [head_id, ...]}（head_id は created_at 昇順・id 昇順でソート済み）
    """
    if not decision_ids:
        return {}

    # source が target を supersede する。newer_adj[target] = [それを supersede する新しい id, ...]
    newer_adj: dict[int, list[int]] = {}
    for r in conn.execute("SELECT source_id, target_id FROM decision_supersedes").fetchall():
        s, t = r["source_id"], r["target_id"]
        newer_adj.setdefault(t, []).append(s)

    def _reach_newer(start: int) -> set[int]:
        reached: set[int] = set()
        visited: set[int] = {start}
        queue: list[int] = [start]
        while queue:
            current = queue.pop(0)
            for nid in newer_adj.get(current, ()):
                if nid not in visited:
                    visited.add(nid)
                    reached.add(nid)
                    queue.append(nid)
        return reached

    all_ids: set[int] = set()
    candidates_by_decision: dict[int, set[int]] = {}
    for did in decision_ids:
        candidates = _reach_newer(did) | {did}
        candidates_by_decision[did] = candidates
        all_ids |= candidates

    order_key: dict[int, tuple] = {}
    if all_ids:
        placeholders = ",".join("?" * len(all_ids))
        for r in conn.execute(
            f"SELECT id, created_at FROM decisions WHERE id IN ({placeholders})",
            tuple(all_ids),
        ).fetchall():
            order_key[r["id"]] = (r["created_at"], r["id"])

    result: dict[int, list[int]] = {}
    for did in decision_ids:
        heads = [cid for cid in candidates_by_decision[did] if not newer_adj.get(cid)]
        heads.sort(key=lambda cid: order_key.get(cid, ("", cid)))
        result[did] = heads
    return result


def annotate_staleness(
    conn: sqlite3.Connection,
    items: list[dict],
    now: datetime | None = None,
) -> None:
    """decision item 群に staleness ブロックを in-place 付与する。

    Args:
        conn: DB 接続。
        items: 各要素は最低限 {"id": int, "created_at": str} を持つ dict。
            "reason" キーがあればアンカー抽出に使う（無ければ anchors キーを省略する）。
        now: 経過日数計算の基準時刻（省略時は UTC now）。

    付与形:
        item["staleness"] = {
            "is_superseded": bool,
            "superseded_by": int | None,   # 最新1hop（get_superseded_by_batch と同一規則）
            "chain_heads": [int, ...],     # 推移的最新。自身が head なら [自身の id]
            "age_days": int,               # created_at からの経過日数
            "anchors": [{"raw","date","commit"}, ...],  # reason があるときのみ付与
        }
    """
    if not items:
        return
    if now is None:
        now = datetime.now(timezone.utc)

    ids = [item["id"] for item in items]
    superseded_by_map = get_superseded_by_batch(conn, ids)
    chain_heads_map = get_chain_heads_batch(conn, ids)

    for item in items:
        did = item["id"]
        superseded_by = superseded_by_map.get(did)
        staleness = {
            "is_superseded": superseded_by is not None,
            "superseded_by": superseded_by,
            "chain_heads": chain_heads_map.get(did, [did]),
            "age_days": _age_days(item["created_at"], now),
        }
        if "reason" in item:
            staleness["anchors"] = _extract_anchors(item.get("reason") or "")
        item["staleness"] = staleness


def _age_days(created_at: str, now: datetime) -> int:
    """created_at（DB の ISO8601 相当文字列）から now までの経過日数を計算する。"""
    created = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
    return max((now - created).days, 0)
