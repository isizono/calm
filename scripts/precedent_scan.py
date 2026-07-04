"""判例定型節（docs/precedent-format.md）の規約準拠状況を計測する read-only ベースラインスクリプト。

decisions テーブル（取り消し済みを除く）全件に `src.services.precedent_pure.parse_precedent_sections`
を適用し、節あり件数・検証アンカー付き件数・warning 分布を集計して出力する。書き込みクエリは
一切発行しない（DB接続は `PRAGMA query_only = ON` + URI `mode=ro` で開く）。

使い方:
    uv run python scripts/precedent_scan.py
    uv run python scripts/precedent_scan.py --format json
    uv run python scripts/precedent_scan.py --db-path /path/to/discussion.db
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.services.* の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.services.precedent_pure import parse_precedent_sections  # noqa: E402

# 既知の warning 文言（precedent_pure.py の warnings.append 呼び出し）をカテゴリに落とす。
# 新しい warning 文言が precedent_pure.py に追加された場合、ここに無いものは "other" に落ちる。
_WARNING_CATEGORY_PREFIXES = (
    ("empty section", "empty_section"),
    ("near-miss heading", "near_miss_heading"),
    ("verification anchor without date", "verification_anchor_without_date"),
    ("rejected alternative without", "rejected_alternative_without_separator"),
)


def _categorize_warning(warning: str) -> str:
    """warning文字列を分布集計用のカテゴリ名に分類する。既知パターンに一致しなければ 'other'。"""
    for prefix, category in _WARNING_CATEGORY_PREFIXES:
        if warning.startswith(prefix):
            return category
    return "other"


def _open_readonly_connection(db_path: str) -> sqlite3.Connection:
    """書き込みクエリを拒否する読み取り専用DB接続を開く。

    URI `mode=ro` に加えて `PRAGMA query_only = ON` を掛ける二重ガード。
    片方だけでは環境（SQLiteのビルドオプション・URI未対応ドライバ等）によって
    すり抜ける可能性があるため両方を掛ける。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def scan_precedents(conn: sqlite3.Connection) -> dict:
    """decisions テーブル（取り消し済みを除く）に定型節パーサを適用し、規約準拠状況を集計する。

    Args:
        conn: 読み取り専用のDB接続。

    Returns:
        {
          "total_decisions": int,               # 取り消し済みを除いた全decision件数
          "with_sections": int,                  # 定型節（正規 or 近似見出し）が1つでも検出された件数
          "without_sections": int,               # 節が検出されなかった件数（legacy本文）
          "with_verification_anchor": int,       # 検証行が1つ以上ある件数
          "warnings_total": int,                 # warning延べ件数
          "warning_counts": {category: int, ...} # カテゴリ別warning件数
        }
    """
    rows = conn.execute(
        "SELECT id, reason FROM decisions WHERE retracted_at IS NULL"
    ).fetchall()

    total = len(rows)
    with_sections = 0
    with_verification_anchor = 0
    warnings_total = 0
    warning_counts: dict[str, int] = {}

    for row in rows:
        parsed = parse_precedent_sections(row["reason"] or "")
        if parsed is None:
            continue
        with_sections += 1
        if parsed["verification_anchors"]:
            with_verification_anchor += 1
        for warning in parsed["warnings"]:
            category = _categorize_warning(warning)
            warning_counts[category] = warning_counts.get(category, 0) + 1
            warnings_total += 1

    return {
        "total_decisions": total,
        "with_sections": with_sections,
        "without_sections": total - with_sections,
        "with_verification_anchor": with_verification_anchor,
        "warnings_total": warnings_total,
        "warning_counts": warning_counts,
    }


def render_text_report(report: dict) -> str:
    """scan_precedentsの結果をテキスト表として整形する。"""
    total = report["total_decisions"]

    def _pct(n: int) -> str:
        return f"{(n / total * 100):.1f}%" if total else "n/a"

    lines = [
        "判例定型節 規約準拠状況スキャン",
        "=" * 40,
        f"対象decision件数（取り消し済み除く）: {total}",
        f"定型節あり:           {report['with_sections']:>6}  ({_pct(report['with_sections'])})",
        f"定型節なし（legacy）: {report['without_sections']:>6}  ({_pct(report['without_sections'])})",
        f"検証アンカー付き:     {report['with_verification_anchor']:>6}  ({_pct(report['with_verification_anchor'])})",
        "",
        f"warning延べ件数: {report['warnings_total']}",
    ]
    if report["warning_counts"]:
        lines.append("warning分布:")
        for category, count in sorted(
            report["warning_counts"].items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"  {category:<40} {count:>6}")
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
    args = parser.parse_args(argv)

    if args.db_path:
        db_path = args.db_path
    else:
        from src.db import get_db_path

        db_path = get_db_path()

    conn = _open_readonly_connection(db_path)
    try:
        report = scan_precedents(conn)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text_report(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
