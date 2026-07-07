"""予算配分の共通プリミティブ。

複数の read tool 予算面（pull_precedents 等）で共通に使う優先度スコア関数と、
予算配分の中核ロジックを集約する。

allocate_decision_budget は precedent_pull_service の既存配分ロジックをそのまま
移設したものであり、挙動は一切変えない。importance_score / recency_score /
relevance_score / size_penalty は今後の予算面拡張のために新設した汎用スコア関数で、
現時点では allocate_decision_budget の配分順序には使われていない
（配分順の決定性を壊さないため）。

予算値はハードコードせず src.config から読む。
"""
import math

from src.config import (
    PRECEDENT_BUDGET_CHARS,
    RECENCY_DECAY_FLOOR,
    RECENCY_DECAY_RATE,
)

# get_config 等から参照する、budget_service が把握する予算関連の既定値一覧。
# 値は src.config の環境変数オーバーライドをそのまま反映する（ハードコードしない）。
BUDGET_DEFAULTS: dict = {
    "precedent_budget_chars": PRECEDENT_BUDGET_CHARS,
    "recency_decay_rate": RECENCY_DECAY_RATE,
    "recency_decay_floor": RECENCY_DECAY_FLOOR,
}


def importance_score(is_pinned: bool = False, weight: float = 1.0) -> float:
    """明示的重要度シグナル（pin等）を0〜weightのスコアに変換する。"""
    return weight if is_pinned else 0.0


def recency_score(age_days: float) -> float:
    """created_at からの経過日数を0〜1の recency スコアに変換する（指数減衰、下限あり）。

    search_service._apply_recency_boost と同じ減衰式・設定値（RECENCY_DECAY_RATE /
    RECENCY_DECAY_FLOOR）を共有する。
    """
    return max(math.exp(-age_days * RECENCY_DECAY_RATE), RECENCY_DECAY_FLOOR)


def relevance_score(rank: int, total: int) -> float:
    """順位ベースの関連度スコア（0〜1、rank=0が最高）。totalが0以下なら0.0を返す。"""
    if total <= 0:
        return 0.0
    return max(0.0, 1.0 - (rank / total))


def size_penalty(size_chars: int, budget_chars: int) -> float:
    """本文サイズが予算に占める割合をペナルティ（0〜1、大きいほど高ペナルティ）として返す。

    budget_chars が0以下のときは1.0（最大ペナルティ）を返す。
    """
    if budget_chars <= 0:
        return 1.0
    return min(1.0, size_chars / budget_chars)


def allocate_decision_budget(
    all_ids: list[int],
    decision_by_id: dict[int, dict],
    supersede_map: dict[int, dict],
    budget_chars: int,
) -> tuple[set[int], int]:
    """配分順（非superseded→新しい順 → superseded→新しい順）に予算内へ detail=full を割り当てる。

    予算に収まらなくなった時点で以降は index 固定にする（配分順への信頼を優先し、
    後続のより小さい項目を先に昇格させるビンパッキングは行わない）。

    precedent_pull_service._allocate_budget の移設（挙動不変）。

    Returns: (full_ids, used_chars)
    """
    order = list(all_ids)
    order.sort(key=lambda did: did, reverse=True)
    order.sort(key=lambda did: decision_by_id[did]["created_at"], reverse=True)
    order.sort(key=lambda did: 1 if supersede_map.get(did, {}).get("is_superseded") else 0)

    full_ids: set[int] = set()
    used = 0
    for did in order:
        dec = decision_by_id[did]
        cost = len(dec.get("decision") or "") + len(dec.get("reason") or "")
        if used + cost > budget_chars:
            break
        full_ids.add(did)
        used += cost
    return full_ids, used
