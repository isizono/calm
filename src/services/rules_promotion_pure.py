"""tag notes に繰り返し出る教訓を検知し、rules/habits等への格上げ候補を洗い出す
pure な層。

「複数タグ重複」（独立な複数タグに同趣旨のセクションがある状態）を候補として抽出する。
DB アクセス・embedding計算等のI/Oは持たない（precedent_pure.py の分離慣行に合わせる）。
呼び出し側（scripts/rules_promotion_scan.py）がタグごとのnotes・embeddingベクトル・
共起カウントを読み取り、本モジュールへ渡す。
"""
import math

from src.services.tag_analysis_service import CLUSTER_PMI_THRESHOLD, calc_pmi
from src.services.tag_service import (
    _DEMOTE_INDEX_HEADING,
    _normalize_section_key,
    _split_tag_notes_layers,
)

__all__ = [
    "INTENT_NAMESPACE",
    "extract_detectable_units",
    "cosine_similarity",
    "find_similar_pairs",
    "is_cooccurring_pair",
    "cluster_candidates",
]

# intent名前空間タグは作業フェーズの行動指示であり、常時注入契約(always_inject)の下にある。
# 繰り返し=誤配置の証拠という本検出器の前提が成立しないため検知対象から外す。
INTENT_NAMESPACE = "intent"

_DEMOTE_INDEX_KEY = _normalize_section_key(_DEMOTE_INDEX_HEADING)


def extract_detectable_units(
    tag_str: str, namespace: str, notes: str, *, archived: bool = False
) -> list[dict]:
    """1タグのtag notesから検知対象の単位を抽出する。

    `## `見出しがあればセクション単位（退避索引セクションを除く）を、無ければ前文全体を
    1単位として扱う（前文が空文字ならその単位も出さない）。intent名前空間のタグと
    archivedタグは検知対象外のため常に空リストを返す。

    Returns:
        [{"tag": tag_str, "heading": str | None, "text": str}, ...]
        headingはNoneのとき前文由来の単位であることを示す。
    """
    if archived or namespace == INTENT_NAMESPACE:
        return []

    layers = _split_tag_notes_layers(notes)
    sections = layers["sections"]

    if sections:
        units = []
        for sec in sections:
            if _normalize_section_key(sec["heading"]) == _DEMOTE_INDEX_KEY:
                continue
            units.append({"tag": tag_str, "heading": sec["heading"], "text": sec["block"]})
        return units

    preamble = layers["preamble"]
    if not preamble.strip():
        return []
    return [{"tag": tag_str, "heading": None, "text": preamble}]


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """2つのembeddingベクトルのコサイン類似度。次元不一致・ゼロベクトルは0.0を返す。"""
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_similar_pairs(
    units: list[dict], embeddings: list[list[float]], threshold: float
) -> list[dict]:
    """異なるタグに属する単位同士で、コサイン類似度が閾値以上のペアを抽出する。

    同一タグ内の単位ペアはここでは対象にしない（同タグ内での統合は別の扱いであり、
    「複数タグ重複」の対象ではないため）。

    Args:
        units: extract_detectable_unitsの出力を集約したリスト。
        embeddings: unitsと同じ順序・同じ長さのembeddingベクトル列。
        threshold: コサイン類似度の下限（この値以上を候補ペアとする）。

    Returns:
        [{"a": unitsのindex, "b": unitsのindex, "similarity": float}, ...]
    """
    if len(units) != len(embeddings):
        raise ValueError("units and embeddings must have the same length")

    pairs = []
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if units[i]["tag"] == units[j]["tag"]:
                continue
            similarity = cosine_similarity(embeddings[i], embeddings[j])
            if similarity >= threshold:
                pairs.append({"a": i, "b": j, "similarity": similarity})
    return pairs


def is_cooccurring_pair(
    tag_a: str,
    tag_b: str,
    co_counts: dict[tuple[str, str], int],
    usage_counts: dict[str, int],
    total: int,
    threshold: float = CLUSTER_PMI_THRESHOLD,
) -> bool:
    """2タグがほぼ常に共起する(独立とみなせない)かをPMIで判定する。

    「独立」の意味の裏返し: PMIが高いほど、2タグは互いの出現を強く予測し合う
    (=ほぼ同じ文脈でしか使われない)。co_countsのキーは(tag_a, tag_b)の順不同ペア。
    """
    key = (tag_a, tag_b) if (tag_a, tag_b) in co_counts else (tag_b, tag_a)
    co_count = co_counts.get(key, 0)
    pmi = calc_pmi(co_count, usage_counts.get(tag_a, 0), usage_counts.get(tag_b, 0), total)
    return pmi >= threshold


def cluster_candidates(
    units: list[dict],
    pairs: list[dict],
    co_counts: dict[tuple[str, str], int],
    usage_counts: dict[str, int],
    total: int,
    cooccurrence_threshold: float = CLUSTER_PMI_THRESHOLD,
) -> list[dict]:
    """類似ペアを、ほぼ常に共起するタグ同士の重複を除いて連結成分にまとめる。

    ほぼ常に一緒に付くタグ同士のペア(is_cooccurring_pairがTrue)は連結せずに捨てる。
    残ったペアで作った連結成分のうち、2タグ以上にまたがるものだけを候補クラスタとする
    (同一タグ内でのみ連結した成分は「同タグ内での統合」であり、この検出器の対象外)。

    Returns:
        [{"tags": [tag_str, ...], "units": [unit, ...], "pairs": [pair, ...]}, ...]
        tagsはソート済みのユニークなタグ文字列リスト。
    """
    parent = list(range(len(units)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    kept_pairs = []
    for pair in pairs:
        tag_a = units[pair["a"]]["tag"]
        tag_b = units[pair["b"]]["tag"]
        if is_cooccurring_pair(tag_a, tag_b, co_counts, usage_counts, total, cooccurrence_threshold):
            continue
        union(pair["a"], pair["b"])
        kept_pairs.append(pair)

    groups: dict[int, list[int]] = {}
    for idx in range(len(units)):
        groups.setdefault(find(idx), []).append(idx)

    clusters = []
    for root, indices in groups.items():
        if len(indices) < 2:
            continue
        tags = sorted({units[i]["tag"] for i in indices})
        if len(tags) < 2:
            continue
        cluster_pairs = [p for p in kept_pairs if find(p["a"]) == root]
        clusters.append(
            {
                "tags": tags,
                "units": [units[i] for i in indices],
                "pairs": cluster_pairs,
            }
        )
    return clusters
