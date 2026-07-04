"""運用計測の突合集計: 巻き戻し率・shadow乖離率・矛盾/miss/誤類推件数

signal_events テーブル（記録先は品質投資コンポーネントの signal_service が正）と、
GO判定パッケージの機械可読ブロック（go_package.py extract / shadow-report が出力する
JSON。--packages-file で受ける）を突合して率指標を計算する、この種の集計の唯一の実装体。
生データを読むだけで、閾値判定・昇格判定は行わない。

Usage:
    uv run python scripts/ops_metrics.py [--window-days 30] [--db <path>] [--json]
                                          [--packages-file <json>]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# プロジェクトルートをパスに追加（src.db等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# 運用計測が読む signal_events の kind。7 種のうち machine_error / friction は
# 汎用の故障・不満報告であり率指標の対象外（品質投資コンポーネントの管轄）。
_CONTRADICTION_RESOLUTIONS = ("existing_correct", "new_correct", "unresolved")

# boundary_case / rollback の context スキーマ（mode / machine_verdict / divergence の
# 許容値）を規定する唯一の箇所。生成側とこの定義がずれると、率指標の分母が 0 になり
# _rate() が黙って N/A を返すだけで計測破綻に気づけない（検知手段のない既知の制約）。
_BOUNDARY_MODE_LIVE = "live"
_BOUNDARY_MODE_SHADOW = "shadow"
_BOUNDARY_VERDICT_POST_VETO = "post_veto_candidate"
_DIVERGENCE_NONE = "none"
_DIVERGENCE_FALSE_NEGATIVE = "false_negative"


def _connect(db_path: str) -> sqlite3.Connection:
    """読み取り専用の軽量接続を作る（sqlite-vec拡張のロードは不要なため素の sqlite3 を使う）。

    他の DB 接続経路（src.db.get_connection / sanitize hook）に揃えて busy_timeout を
    設定し、WAL チェックポイント等での一時ロックに即エラーを返さずリトライ待機させる。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fetch_signals(conn: sqlite3.Connection, kind: str, window_days: Optional[int]) -> list[dict]:
    """指定 kind の signal_events 行を取得し、context/refs を JSON パースして返す。

    window_days が None のときは全期間、指定時は first_seen_at が
    直近 window_days 日以内の行に絞る。同一案件の再報告は fingerprint dedup
    により1行に集約されている前提のため、ここでは行数=件数として扱う
    （occurrence_count は「同じ事象が何度報告されたか」であり「事象が何件起きたか」
    ではない）。
    """
    query = "SELECT * FROM signal_events WHERE kind = ?"
    params: list[object] = [kind]
    if window_days is not None:
        query += " AND first_seen_at >= datetime('now', ?)"
        params.append(f"-{window_days} days")

    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["context"] = json.loads(d["context"]) if d.get("context") else {}
        d["refs"] = json.loads(d["refs"]) if d.get("refs") else None
        result.append(d)
    return result


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """denominator が 0 のとき None（N/A）を返し、ゼロ除算を避ける。"""
    if denominator == 0:
        return None
    return numerator / denominator


def _contradiction_metrics(conn: sqlite3.Connection, window_days: Optional[int]) -> dict:
    """矛盾イベント数と resolution 内訳を返す。"""
    rows = _fetch_signals(conn, "contradiction", window_days)
    by_resolution = {res: 0 for res in _CONTRADICTION_RESOLUTIONS}
    by_resolution["unknown"] = 0
    for row in rows:
        resolution = (row["context"] or {}).get("resolution")
        if resolution in by_resolution:
            by_resolution[resolution] += 1
        else:
            by_resolution["unknown"] += 1
    return {"count": len(rows), "by_resolution": by_resolution}


def _rollback_metrics(
    conn: sqlite3.Connection,
    window_days: Optional[int],
    boundary_rows: list[dict],
) -> dict:
    """巻き戻し率 = rollback件数 / boundary_case(mode=live, machine_verdict=post_veto_candidate)件数。

    分子(rollback)と分母(boundary_case)は案件IDでリンクしておらず、それぞれ
    first_seen_at の window で独立に絞り込むだけである。境界案件の記録時点と
    rollback 時点が window 境界をまたぐと、分子が分母を上回るなど実態と乖離した
    値になり得る。案件IDによる突合は生成側（境界ゲート）が未実装のため、現状は
    独立カウントの近似値として扱う（既知の制約）。
    """
    rollback_rows = _fetch_signals(conn, "rollback", window_days)
    denom_rows = [
        row
        for row in boundary_rows
        if (row["context"] or {}).get("mode") == _BOUNDARY_MODE_LIVE
        and (row["context"] or {}).get("machine_verdict") == _BOUNDARY_VERDICT_POST_VETO
    ]
    numerator = len(rollback_rows)
    denominator = len(denom_rows)
    return {
        "rollback_count": numerator,
        "post_veto_live_count": denominator,
        "rate": _rate(numerator, denominator),
    }


def _shadow_divergence_metrics(boundary_rows: list[dict]) -> dict:
    """shadow乖離率 = boundary_case(mode=shadow)のうちdivergence!=noneの割合。false_negativeは別掲。"""
    shadow_rows = [
        row for row in boundary_rows if (row["context"] or {}).get("mode") == _BOUNDARY_MODE_SHADOW
    ]
    total = len(shadow_rows)
    diverged = [
        row
        for row in shadow_rows
        if (row["context"] or {}).get("divergence", _DIVERGENCE_NONE) != _DIVERGENCE_NONE
    ]
    false_negative = [
        row
        for row in shadow_rows
        if (row["context"] or {}).get("divergence") == _DIVERGENCE_FALSE_NEGATIVE
    ]
    return {
        "shadow_total": total,
        "diverged_count": len(diverged),
        "divergence_rate": _rate(len(diverged), total),
        "false_negative_count": len(false_negative),
        "false_negative_rate": _rate(len(false_negative), total),
    }


def _sum_citation_slots(packages: list[dict]) -> int:
    """--packages-file の各パッケージについて precedents 件数 + pull.presented 件数
    （presented がリストのときのみ。'unavailable' 等の文字列は対象外）を合算する。

    pull hit 率の分母（「判例引用が提示・引用された機会の総数」）として使う。
    """
    total = 0
    for package in packages:
        precedents = package.get("precedents") or []
        total += len(precedents)
        presented = (package.get("pull") or {}).get("presented")
        if isinstance(presented, list):
            total += len(presented)
    return total


def _count_applied_citations(packages: list[dict]) -> int:
    """--packages-file の precedents のうち stance=applied の件数を合算する（誤類推率の分母）。"""
    total = 0
    for package in packages:
        for precedent in package.get("precedents") or []:
            if precedent.get("stance") == "applied":
                total += 1
    return total


def _pull_metrics(conn: sqlite3.Connection, window_days: Optional[int], packages: Optional[list[dict]]) -> dict:
    """pull miss 件数 / hit率。--packages-file 未供給時は件数のみ返す。"""
    miss_rows = _fetch_signals(conn, "precedent_miss", window_days)
    result: dict = {"miss_count": len(miss_rows)}
    if packages is not None:
        denominator = _sum_citation_slots(packages)
        result["citation_slot_count"] = denominator
        result["miss_rate"] = _rate(len(miss_rows), denominator)
    return result


def _misapplied_metrics(conn: sqlite3.Connection, window_days: Optional[int], packages: Optional[list[dict]]) -> dict:
    """誤類推件数 / 誤類推率。--packages-file 未供給時は件数のみ返す。"""
    misapplied_rows = _fetch_signals(conn, "precedent_misapplied", window_days)
    result: dict = {"misapplied_count": len(misapplied_rows)}
    if packages is not None:
        denominator = _count_applied_citations(packages)
        result["applied_citation_count"] = denominator
        result["misapplied_rate"] = _rate(len(misapplied_rows), denominator)
    return result


def compute_metrics(
    db_path: str,
    window_days: Optional[int] = 30,
    packages: Optional[list[dict]] = None,
) -> dict:
    """signal_events (+ 供給時は packages) を読み、率指標の突合集計結果を返す。

    Args:
        db_path: signal_events を含む SQLite DB のパス
        window_days: 集計対象期間（日数）。None のとき全期間を対象にする
        packages: go_package.py extract/shadow-report が出力する go-package
            機械可読ブロックの一覧（パース済み）。None のとき pull hit率・誤類推率は
            件数のみ返し、率は計算しない

    Returns:
        矛盾イベント数・巻き戻し率・shadow乖離率・pull miss・誤類推の集計結果
    """
    conn = _connect(db_path)
    try:
        # rollback率と shadow乖離率はどちらも同一 kind・同一 window の boundary_case を
        # 参照するため、ここで一度だけ取得して両者へ渡す。
        boundary_rows = _fetch_signals(conn, "boundary_case", window_days)
        return {
            "window_days": window_days,
            "contradiction": _contradiction_metrics(conn, window_days),
            "rollback": _rollback_metrics(conn, window_days, boundary_rows),
            "shadow_divergence": _shadow_divergence_metrics(boundary_rows),
            "pull": _pull_metrics(conn, window_days, packages),
            "precedent_misapplied": _misapplied_metrics(conn, window_days, packages),
        }
    finally:
        conn.close()


def load_packages(packages_file: Optional[str]) -> Optional[list[dict]]:
    """--packages-file を読み込みパースする。未指定時は None を返す。

    ファイルは go-package 機械可読ブロックの JSON 配列でなければならない。
    """
    if packages_file is None:
        return None
    text = Path(packages_file).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("--packages-file must contain a JSON array of go-package blocks")
    if not all(isinstance(item, dict) for item in data):
        raise ValueError("--packages-file の各要素は go-package オブジェクト(JSON object)でなければなりません")
    return data


def _format_rate(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def format_text(metrics: dict) -> str:
    """人間向けテキストレポートを組み立てる。"""
    window_days = metrics["window_days"]
    window_label = "全期間" if window_days is None else f"直近{window_days}日"
    lines = [f"=== ops_metrics ({window_label}) ==="]

    c = metrics["contradiction"]
    lines.append(
        f"矛盾イベント数: {c['count']} 件"
        f" (existing_correct={c['by_resolution']['existing_correct']}"
        f", new_correct={c['by_resolution']['new_correct']}"
        f", unresolved={c['by_resolution']['unresolved']}"
        f", unknown={c['by_resolution']['unknown']})"
    )

    r = metrics["rollback"]
    lines.append(
        f"巻き戻し率: {_format_rate(r['rate'])}"
        f" ({r['rollback_count']}/{r['post_veto_live_count']})"
    )

    s = metrics["shadow_divergence"]
    lines.append(
        f"shadow乖離率: {_format_rate(s['divergence_rate'])}"
        f" ({s['diverged_count']}/{s['shadow_total']})"
        f"  うちfalse_negative: {_format_rate(s['false_negative_rate'])}"
        f" ({s['false_negative_count']}/{s['shadow_total']})"
    )

    p = metrics["pull"]
    if "miss_rate" in p:
        lines.append(
            f"pull miss率: {_format_rate(p['miss_rate'])}"
            f" ({p['miss_count']}/{p['citation_slot_count']})"
        )
    else:
        lines.append(f"pull miss件数: {p['miss_count']} 件（--packages-file 未供給のため率は算出不可）")

    m = metrics["precedent_misapplied"]
    if "misapplied_rate" in m:
        lines.append(
            f"誤類推率: {_format_rate(m['misapplied_rate'])}"
            f" ({m['misapplied_count']}/{m['applied_citation_count']})"
        )
    else:
        lines.append(f"誤類推件数: {m['misapplied_count']} 件（--packages-file 未供給のため率は算出不可）")

    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="signal_events + go-package抽出データの突合集計（巻き戻し率・shadow乖離率・矛盾/miss/誤類推件数）"
    )
    parser.add_argument(
        "--window-days", type=int, default=30,
        help="集計対象期間（日数、デフォルト30）。0以下を指定すると全期間を対象にする",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="DBファイルパス（省略時は src.db.get_db_path() の解決先を使う）",
    )
    parser.add_argument("--json", action="store_true", help="JSON形式で出力する")
    parser.add_argument(
        "--packages-file", type=str, default=None,
        help="go_package.py extract/shadow-report が出力するgo-package機械可読ブロックのJSON配列ファイル",
    )
    args = parser.parse_args(argv)

    db_path = args.db
    if db_path is None:
        from src.db import get_db_path

        db_path = get_db_path()

    window_days = args.window_days if args.window_days and args.window_days > 0 else None

    try:
        packages = load_packages(args.packages_file)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"--packages-file の読み込みに失敗しました: {e}", file=sys.stderr)
        return 1

    # load_packages はトップレベルが dict の配列であることまでしか検証しない。
    # precedents/pull など入れ子のフォーマット逸脱は集計中に AttributeError/
    # TypeError として現れるため、素の traceback を出さず制御されたエラーに変換する。
    try:
        metrics = compute_metrics(db_path, window_days=window_days, packages=packages)
    except (AttributeError, TypeError) as e:
        print(f"--packages-file のデータ形式が不正です: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(format_text(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
