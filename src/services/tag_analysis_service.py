"""タグ分析サービス: 共起分析・クラスタリング・孤児検出・重複候補検出"""
import logging
import math
from collections import defaultdict

from src.db import get_connection

logger = logging.getLogger(__name__)

# 共起計算に使用するjunction table定義
# (table_name, entity_column)
_JUNCTION_TABLES = [
    ("topic_tags", "topic_id"),
    ("activity_tags", "activity_id"),
    ("decision_tags", "decision_id"),
    ("log_tags", "log_id"),
]

# クラスタリングのPMI閾値
CLUSTER_PMI_THRESHOLD = 2.0

# 重複候補検出のコサイン距離閾値（tag_service.MERGE_THRESHOLDと同じ）
DUPLICATE_DISTANCE_THRESHOLD = 0.15


def _get_tag_usage_counts(conn, domain_tag_id=None, domain_ids=None):
    """各タグのusage_count（出現エンティティ数）を取得する。

    Args:
        conn: DB接続
        domain_tag_id: domainフィルタ用タグID。指定時はそのdomainタグと
                       共起するエンティティに限定する
        domain_ids: 除外するdomain:タグのIDセット。Noneの場合は除外しない

    Returns:
        {tag_id: usage_count}
    """
    counts = defaultdict(int)

    for table, entity_col in _JUNCTION_TABLES:
        if domain_tag_id is not None:
            sql = f"""
                SELECT jt.tag_id, COUNT(DISTINCT jt.{entity_col}) AS cnt
                FROM {table} jt
                WHERE jt.{entity_col} IN (
                    SELECT {entity_col} FROM {table} WHERE tag_id = ?
                )
                GROUP BY jt.tag_id
            """
            rows = conn.execute(sql, (domain_tag_id,)).fetchall()
        else:
            sql = f"""
                SELECT tag_id, COUNT(DISTINCT {entity_col}) AS cnt
                FROM {table}
                GROUP BY tag_id
            """
            rows = conn.execute(sql).fetchall()

        for row in rows:
            counts[row["tag_id"]] += row["cnt"]

    if domain_ids:
        counts = {tid: cnt for tid, cnt in counts.items() if tid not in domain_ids}

    return dict(counts)


def _get_co_occurrence_counts(conn, domain_tag_id=None, domain_ids=None):
    """タグペアの共起カウントを集計する。

    Args:
        conn: DB接続
        domain_tag_id: domainフィルタ用タグID
        domain_ids: 除外するdomain:タグのIDセット。Noneの場合は除外しない

    Returns:
        {(tag_a, tag_b): co_count} (tag_a < tag_b)
    """
    co_counts = defaultdict(int)

    for table, entity_col in _JUNCTION_TABLES:
        if domain_tag_id is not None:
            sql = f"""
                SELECT t1.tag_id AS tag_a, t2.tag_id AS tag_b, COUNT(*) AS co_count
                FROM {table} t1
                JOIN {table} t2 ON t1.{entity_col} = t2.{entity_col} AND t1.tag_id < t2.tag_id
                WHERE t1.{entity_col} IN (
                    SELECT {entity_col} FROM {table} WHERE tag_id = ?
                )
                GROUP BY t1.tag_id, t2.tag_id
            """
            rows = conn.execute(sql, (domain_tag_id,)).fetchall()
        else:
            sql = f"""
                SELECT t1.tag_id AS tag_a, t2.tag_id AS tag_b, COUNT(*) AS co_count
                FROM {table} t1
                JOIN {table} t2 ON t1.{entity_col} = t2.{entity_col} AND t1.tag_id < t2.tag_id
                GROUP BY t1.tag_id, t2.tag_id
            """
            rows = conn.execute(sql).fetchall()

        for row in rows:
            key = (row["tag_a"], row["tag_b"])
            co_counts[key] += row["co_count"]

    if domain_ids:
        co_counts = {
            (a, b): cnt for (a, b), cnt in co_counts.items()
            if a not in domain_ids and b not in domain_ids
        }

    return dict(co_counts)


def calc_pmi(co_count, count_a, count_b, total):
    """PMI = log2(P(a,b) / (P(a) * P(b)))

    Args:
        co_count: タグペアの共起数
        count_a: タグAの出現数
        count_b: タグBの出現数
        total: 全エンティティ数

    Returns:
        PMI値（float）
    """
    if total == 0 or count_a == 0 or count_b == 0 or co_count == 0:
        return 0.0
    p_ab = co_count / total
    p_a = count_a / total
    p_b = count_b / total
    return math.log2(p_ab / (p_a * p_b))


def _get_total_entities(conn, domain_tag_id=None):
    """全エンティティ数（各junction tableのユニークエンティティ数の合計）を取得する。

    PMIの確率計算の分母として使う。
    """
    total = 0
    for table, entity_col in _JUNCTION_TABLES:
        if domain_tag_id is not None:
            sql = f"""
                SELECT COUNT(DISTINCT {entity_col}) AS cnt
                FROM {table}
                WHERE {entity_col} IN (
                    SELECT {entity_col} FROM {table} WHERE tag_id = ?
                )
            """
            rows = conn.execute(sql, (domain_tag_id,)).fetchall()
        else:
            sql = f"SELECT COUNT(DISTINCT {entity_col}) AS cnt FROM {table}"
            rows = conn.execute(sql).fetchall()
        total += rows[0]["cnt"]
    return total


def _build_tag_maps(conn, tag_ids):
    """tag_id → 表示名・詳細情報のマッピングを構築する。

    Returns:
        (tag_names, tag_info)
        tag_names: {tag_id: "namespace:name" or "name"}
        tag_info: {tag_id: {"namespace": str, "name": str, "archived": bool,
                             "archived_reason": str | None}}
    """
    if not tag_ids:
        return {}, {}
    placeholders = ",".join("?" * len(tag_ids))
    rows = conn.execute(
        f"SELECT id, namespace, name, archived_at, archived_reason FROM tags WHERE id IN ({placeholders})",
        tuple(tag_ids),
    ).fetchall()
    tag_names = {}
    tag_info = {}
    for row in rows:
        ns = row["namespace"]
        name = row["name"]
        tag_names[row["id"]] = f"{ns}:{name}" if ns else name
        tag_info[row["id"]] = {
            "namespace": ns,
            "name": name,
            "archived": row["archived_at"] is not None,
            "archived_reason": row["archived_reason"],
        }
    return tag_names, tag_info


def _compute_co_occurrences(co_counts, usage_counts, total, tag_names, focus_tag_id, top_n):
    """共起ペアのPMIを計算しランキングする。

    Args:
        co_counts: {(tag_a, tag_b): co_count}
        usage_counts: {tag_id: usage_count}
        total: 全エンティティ数
        tag_names: {tag_id: tag_string}
        focus_tag_id: focus_tagのID（指定時はそのタグを含むペアのみ）
        top_n: 返すペア数の上限

    Returns:
        [{"tag_a": str, "tag_b": str, "pmi": float, "raw_count": int}, ...]
    """
    results = []
    for (tag_a, tag_b), co_count in co_counts.items():
        # focus_tagフィルタ
        if focus_tag_id is not None:
            if tag_a != focus_tag_id and tag_b != focus_tag_id:
                continue

        count_a = usage_counts.get(tag_a, 0)
        count_b = usage_counts.get(tag_b, 0)
        pmi = calc_pmi(co_count, count_a, count_b, total)

        name_a = tag_names.get(tag_a, str(tag_a))
        name_b = tag_names.get(tag_b, str(tag_b))

        results.append({
            "tag_a": name_a,
            "tag_b": name_b,
            "pmi": round(pmi, 2),
            "raw_count": co_count,
        })

    # PMI降順でソート
    results.sort(key=lambda x: x["pmi"], reverse=True)
    return results[:top_n]


def _find_clusters(co_counts, usage_counts, total, tag_names, threshold=CLUSTER_PMI_THRESHOLD):
    """PMI閾値ベースの連結成分検出（Union-Find）。

    Args:
        co_counts: {(tag_a, tag_b): co_count}
        usage_counts: {tag_id: usage_count}
        total: 全エンティティ数
        tag_names: {tag_id: tag_string}
        threshold: PMI閾値

    Returns:
        [{"tags": [str, ...], "cohesion": float}, ...]
    """
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # PMIを計算してエッジを構築
    edges = []
    for (tag_a, tag_b), co_count in co_counts.items():
        count_a = usage_counts.get(tag_a, 0)
        count_b = usage_counts.get(tag_b, 0)
        pmi = calc_pmi(co_count, count_a, count_b, total)
        edges.append((tag_a, tag_b, pmi))

    # 閾値以上のエッジでUnion
    for tag_a, tag_b, pmi in edges:
        if pmi >= threshold:
            if tag_a not in parent:
                parent[tag_a] = tag_a
            if tag_b not in parent:
                parent[tag_b] = tag_b
            union(tag_a, tag_b)

    # グループ化
    clusters_map = defaultdict(set)
    for tag in parent:
        root = find(tag)
        clusters_map[root].add(tag)

    # クラスタごとのPMI値を1パスで集約
    cluster_pmis = defaultdict(list)
    for tag_a, tag_b, pmi in edges:
        if pmi >= threshold and tag_a in parent and tag_b in parent:
            root = find(tag_a)
            cluster_pmis[root].append(pmi)

    # クラスタ結果を構築
    results = []
    for root, members in clusters_map.items():
        if len(members) < 2:
            continue

        pmis = cluster_pmis.get(root, [])
        cohesion = sum(pmis) / len(pmis) if pmis else 0.0

        tag_strings = sorted(
            [tag_names.get(tid, str(tid)) for tid in members]
        )
        results.append({
            "tags": tag_strings,
            "cohesion": round(cohesion, 2),
        })

    # cohesion降順
    results.sort(key=lambda x: x["cohesion"], reverse=True)
    return results


def _find_orphans(usage_counts, co_counts, total, tag_names, tag_info, min_usage):
    """孤児タグを検出する。

    usage < min_usage のタグを孤児とし、最近傍タグ（PMIが最大のペア相手）を付与する。

    Args:
        usage_counts: {tag_id: usage_count}
        co_counts: {(tag_a, tag_b): co_count}
        total: 全エンティティ数
        tag_names: {tag_id: tag_string}
        tag_info: {tag_id: {"archived": bool, "archived_reason": str|None, ...}}
        min_usage: 孤児判定の閾値

    Returns:
        [{"tag": str, "usage": int, "nearest": str|None, "pmi_to_nearest": float|None,
          "archived": bool, "archived_reason": str|None}, ...]
    """
    orphan_ids = {tid for tid, cnt in usage_counts.items() if cnt < min_usage}

    if not orphan_ids:
        return []

    # 隣接マップを事前構築（O(m) → 各孤児の探索はO(degree)）
    adjacency = defaultdict(list)
    for (tag_a, tag_b), co_count in co_counts.items():
        adjacency[tag_a].append((tag_b, co_count))
        adjacency[tag_b].append((tag_a, co_count))

    results = []
    for orphan_id in orphan_ids:
        name = tag_names.get(orphan_id, str(orphan_id))
        usage = usage_counts[orphan_id]

        # 最近傍タグを探す（PMIが最大の相手）
        best_pmi = None
        best_neighbor = None
        for neighbor_id, co_count in adjacency.get(orphan_id, []):
            count_orphan = usage_counts.get(orphan_id, 0)
            count_neighbor = usage_counts.get(neighbor_id, 0)
            pmi = calc_pmi(co_count, count_orphan, count_neighbor, total)
            if best_pmi is None or pmi > best_pmi:
                best_pmi = pmi
                best_neighbor = neighbor_id

        info = tag_info.get(orphan_id, {})
        result = {
            "tag": name,
            "usage": usage,
            "nearest": tag_names.get(best_neighbor, None) if best_neighbor is not None else None,
            "pmi_to_nearest": round(best_pmi, 2) if best_pmi is not None else None,
            "archived": info.get("archived", False),
            "archived_reason": info.get("archived_reason"),
        }
        results.append(result)

    # usage昇順
    results.sort(key=lambda x: x["usage"])
    return results


def _find_suspected_duplicates(tag_ids, tag_names, tag_info):
    """重複候補を検出する（embedding KNN検索）。

    各タグについてKNN検索し、同namespace内で距離が近いものをグルーピングする。
    embedding無効時は空配列を返す。

    Args:
        tag_ids: 分析対象タグIDリスト
        tag_names: {tag_id: tag_string}
        tag_info: {tag_id: {"namespace": str, "name": str}}

    Returns:
        [{"tags": [str, ...], "reason": "high_name_similarity"}, ...]
    """
    try:
        from src.services.embedding_service import search_similar_tags
    except ImportError:
        return []

    if not tag_ids:
        return []

    grouped = set()
    duplicates = []

    for tid in tag_ids:
        if tid in grouped:
            continue
        info = tag_info.get(tid)
        if not info:
            continue

        try:
            similar = search_similar_tags(info["name"], k=10)
        except Exception:
            continue

        if not similar:
            continue

        group = {tid}
        for candidate_id, distance in similar:
            if candidate_id == tid:
                continue
            if distance >= DUPLICATE_DISTANCE_THRESHOLD:
                continue
            if candidate_id not in tag_info:
                continue
            # 同namespace内のみ
            if tag_info[candidate_id]["namespace"] != info["namespace"]:
                continue
            group.add(candidate_id)

        if len(group) > 1:
            grouped.update(group)
            tag_strings = sorted(
                [tag_names.get(t, str(t)) for t in group]
            )
            duplicates.append({
                "tags": tag_strings,
                "reason": "high_name_similarity",
            })

    return duplicates


def _resolve_domain_tag_id(conn, domain):
    """domain文字列からtag_idを解決する。

    Args:
        conn: DB接続
        domain: ドメイン名（例: "calm"）。"domain:"プレフィックスは不要

    Returns:
        tag_id or None。エイリアスタグを指定された場合はcanonical側のtag_id
    """
    row = conn.execute(
        "SELECT id, canonical_id FROM tags WHERE namespace = 'domain' AND name = ?",
        (domain,),
    ).fetchone()
    if row is None:
        return None
    return row["canonical_id"] if row["canonical_id"] is not None else row["id"]


def _resolve_focus_tag_id(conn, focus_tag):
    """focus_tag文字列からtag_idを解決する。

    Args:
        conn: DB接続
        focus_tag: タグ文字列（例: "hook-system"、"intent:design"）

    Returns:
        tag_id or None。エイリアスタグを指定された場合はcanonical側のtag_id
    """
    if ":" in focus_tag:
        ns, name = focus_tag.split(":", 1)
    else:
        ns, name = "", focus_tag

    row = conn.execute(
        "SELECT id, canonical_id FROM tags WHERE namespace = ? AND name = ?",
        (ns, name),
    ).fetchone()
    if row is None:
        return None
    return row["canonical_id"] if row["canonical_id"] is not None else row["id"]


def analyze_tags(
    domain=None,
    include_domain_tags=False,
    focus_tag=None,
    min_usage=2,
    top_n=20,
):
    """タグの共起分析を実行する。

    PMIで共起の重みを計算し、クラスタ検出・孤児タグ検出・重複候補検出を行う。

    Args:
        domain: domainフィルタ（例: "calm"）。指定時はそのdomainに属する
                エンティティのみを分析対象にする
        include_domain_tags: Trueの場合、domain:タグも分析対象に含める。
                             デフォルトはFalse（domain:タグは除外）
        focus_tag: 特定タグにフォーカス。指定時はco_occurrencesをそのタグを含む
                   ペアのみに絞る
        min_usage: 孤児判定の閾値。usage_countがこの値未満のタグを孤児とする
        top_n: co_occurrencesの返却件数上限

    Returns:
        {
            "co_occurrences": [...],
            "clusters": [...],
            "orphans": [...],
            "suspected_duplicates": [...]
        }
    """
    conn = None
    try:
        conn = get_connection()

        # domainフィルタの解決
        domain_tag_id = None
        if domain is not None:
            domain_tag_id = _resolve_domain_tag_id(conn, domain)
            if domain_tag_id is None:
                return {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": f"Domain tag 'domain:{domain}' not found",
                    }
                }

        # focus_tagの解決
        focus_tag_id = None
        if focus_tag is not None:
            focus_tag_id = _resolve_focus_tag_id(conn, focus_tag)
            if focus_tag_id is None:
                return {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": f"Tag '{focus_tag}' not found",
                    }
                }

        # domain:タグIDの事前取得（1回のみ、両関数で共有）
        domain_ids = None
        if not include_domain_tags:
            rows = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain'"
            ).fetchall()
            domain_ids = {row["id"] for row in rows}

        # 1. usage_count取得
        usage_counts = _get_tag_usage_counts(conn, domain_tag_id, domain_ids)

        # 2. 共起行列計算
        co_counts = _get_co_occurrence_counts(conn, domain_tag_id, domain_ids)

        # 3. 全エンティティ数
        total = _get_total_entities(conn, domain_tag_id)

        # タグ名・詳細情報マッピング（1回のクエリで両方構築）
        all_tag_ids = set(usage_counts.keys())
        for tag_a, tag_b in co_counts.keys():
            all_tag_ids.add(tag_a)
            all_tag_ids.add(tag_b)
        tag_names, tag_info = _build_tag_maps(conn, list(all_tag_ids))

        # 4. 共起ペア+PMI
        co_occurrences = _compute_co_occurrences(
            co_counts, usage_counts, total, tag_names, focus_tag_id, top_n,
        )

        # 5. クラスタリング
        clusters = _find_clusters(co_counts, usage_counts, total, tag_names)

        # 6. 孤児検出
        orphans = _find_orphans(usage_counts, co_counts, total, tag_names, tag_info, min_usage)

        # 7. 重複候補検出
        analysis_tag_ids = list(usage_counts.keys())
        suspected_duplicates = _find_suspected_duplicates(
            analysis_tag_ids, tag_names, tag_info,
        )

        return {
            "co_occurrences": co_occurrences,
            "clusters": clusters,
            "orphans": orphans,
            "suspected_duplicates": suspected_duplicates,
        }

    except Exception as e:
        logger.exception("analyze_tags failed")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        if conn:
            conn.close()
