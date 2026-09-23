"""tag notes教訓のrules格上げ機構: 読み取り専用の初回棚卸しスキャン。

全タグのnotesを `src.services.rules_promotion_pure.extract_detectable_units` で
検知対象単位（セクション/前文）に分解し、embeddingのコサイン類似度と共起PMIで
「複数タグ重複」の候補クラスタを洗い出す。書き込みクエリは一切発行しない
（DB接続は `PRAGMA query_only = ON` + URI `mode=ro` で開く。precedent_scan.pyと同型）。

embeddingサーバーが起動できない場合は類似判定をスキップし、結果に `degraded: true` を
付けて返す（書き込みは無いため安全側に倒して空クラスタを返すだけでよい）。

使い方:
    uv run python scripts/rules_promotion_scan.py
    uv run python scripts/rules_promotion_scan.py --format json
    uv run python scripts/rules_promotion_scan.py --db-path /path/to/discussion.db
    uv run python scripts/rules_promotion_scan.py --similarity-threshold 0.8 --pmi-threshold 2.5
"""
import argparse
import json
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.services.* の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.precedent_scan import _open_readonly_connection  # noqa: E402
from src.services.rules_promotion_pure import (  # noqa: E402
    CLUSTER_PMI_THRESHOLD,
    cluster_candidates,
    extract_detectable_units,
    find_similar_pairs,
)

# 既存のタグ重複検出（tag_analysis_service）のコサイン距離閾値0.15
# （=類似度換算で概ね0.85）を出発点として流用する。設計案どおり閾値の較正は
# 本スキャンの実行結果を見てから行う前提であり、この値は初期値にすぎない。
_DEFAULT_SIMILARITY_THRESHOLD = 0.85


def _tag_str(namespace: str, name: str) -> str:
    return f"{namespace}:{name}" if namespace else name


def _load_tag_rows(conn) -> list:
    return conn.execute("SELECT id, namespace, name, notes, archived_at FROM tags").fetchall()


def collect_units(conn) -> list[dict]:
    """全タグのtag notesから検知対象単位を集める。"""
    units = []
    for row in _load_tag_rows(conn):
        tag_str = _tag_str(row["namespace"], row["name"])
        units.extend(
            extract_detectable_units(
                tag_str,
                row["namespace"],
                row["notes"] or "",
                archived=row["archived_at"] is not None,
            )
        )
    return units


def collect_cooccurrence(conn) -> tuple[dict, dict, int]:
    """全タグの共起カウント・使用回数・総エンティティ数をタグ文字列キーで集める。"""
    from src.services.tag_analysis_service import (
        _get_co_occurrence_counts,
        _get_tag_usage_counts,
        _get_total_entities,
    )

    tag_rows = _load_tag_rows(conn)
    id_to_str = {row["id"]: _tag_str(row["namespace"], row["name"]) for row in tag_rows}

    co_counts_raw = _get_co_occurrence_counts(conn)
    usage_counts_raw = _get_tag_usage_counts(conn)
    total = _get_total_entities(conn)

    co_counts = {
        (id_to_str[a], id_to_str[b]): c
        for (a, b), c in co_counts_raw.items()
        if a in id_to_str and b in id_to_str
    }
    usage_counts = {id_to_str[tid]: c for tid, c in usage_counts_raw.items() if tid in id_to_str}
    return co_counts, usage_counts, total


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """embeddingサーバーが使えればテキスト列をまとめてembeddingする。使えなければNone。"""
    from src.services.embedding_service import _encode_batch, _ensure_initialized

    if not texts:
        return []
    if not _ensure_initialized():
        return None
    return _encode_batch(texts, "document")


def scan_candidates(
    conn,
    similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    pmi_threshold: float = CLUSTER_PMI_THRESHOLD,
) -> dict:
    """read-only接続を受け取り、複数タグ重複の候補クラスタを集計する。

    Args:
        conn: 読み取り専用のDB接続。
        similarity_threshold: 類似ペアと判定するコサイン類似度の下限。
        pmi_threshold: 「ほぼ常に共起する」として候補から除外するPMIの下限。

    Returns:
        {"degraded": bool, "unit_count": int, "clusters": [...]}
        degraded=Trueのときembeddingサーバーが使えず類似判定をスキップした
        （clustersは常に空）。
    """
    units = collect_units(conn)
    if not units:
        return {"degraded": False, "unit_count": 0, "clusters": []}

    embeddings = _embed_texts([u["text"] for u in units])
    if embeddings is None:
        return {"degraded": True, "unit_count": len(units), "clusters": []}

    co_counts, usage_counts, total = collect_cooccurrence(conn)
    pairs = find_similar_pairs(units, embeddings, similarity_threshold)
    clusters = cluster_candidates(units, pairs, co_counts, usage_counts, total, pmi_threshold)
    return {"degraded": False, "unit_count": len(units), "clusters": clusters}


def render_text_report(report: dict) -> str:
    """scan_candidatesの結果をテキスト表として整形する。"""
    lines = [
        "rules格上げ候補 初回棚卸しスキャン",
        "=" * 40,
        f"検知対象ユニット数: {report['unit_count']}",
    ]
    if report["degraded"]:
        lines.append("embeddingサーバーに接続できず、類似判定をスキップしました（degraded）。")
        return "\n".join(lines)

    clusters = report["clusters"]
    cutoff_note = "3件未満のため2本目PRの打ち切りを検討可" if len(clusters) < 3 else "3件以上"
    lines.append(f"複数タグにまたがる候補クラスタ数: {len(clusters)}（{cutoff_note}）")

    for i, cluster in enumerate(clusters, 1):
        lines.append("")
        lines.append(f"候補{i}: タグ {', '.join(cluster['tags'])}")
        for unit in cluster["units"]:
            heading = unit["heading"] or "(前文)"
            first_line = next((ln for ln in unit["text"].splitlines() if ln.strip()), "")
            lines.append(f"  - {unit['tag']} / {heading}: {first_line.strip()[:60]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=None,
        help="スキャン対象DBのパス。省略時は src.db.get_db_path() の既定値を使う。",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="出力形式（デフォルト: text）",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=_DEFAULT_SIMILARITY_THRESHOLD,
        help=f"類似ペアと判定するコサイン類似度の下限（デフォルト: {_DEFAULT_SIMILARITY_THRESHOLD}）",
    )
    parser.add_argument(
        "--pmi-threshold",
        type=float,
        default=CLUSTER_PMI_THRESHOLD,
        help=f"ほぼ常に共起するとみなすPMIの下限（デフォルト: {CLUSTER_PMI_THRESHOLD}）",
    )
    args = parser.parse_args(argv)

    if args.db_path:
        db_path = args.db_path
    else:
        from src.db import get_db_path

        db_path = get_db_path()

    conn = _open_readonly_connection(db_path)
    try:
        report = scan_candidates(conn, args.similarity_threshold, args.pmi_threshold)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text_report(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
