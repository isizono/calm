#!/usr/bin/env python3
"""PR サイズ検査: diff の本体コード行・ファイル数を計測し ok/large/oversized を判定する。

ローカル自己チェックと CI 判定の両モードを 1 スクリプトで持つ:

    uv run python scripts/pr_size_check.py --local [--base origin/main] [--json]
    uv run python scripts/pr_size_check.py --ci    # GitHub Actions 上。PR コメント投稿まで行う

サイズ計数関数 (`count_diff_size`) と閾値定数 (`MAX_LINES` / `MAX_FILES`) は
`scripts/gate_check.py` を単一ソースとして import する。母集団定義(tests/・docs/・
uv.lock を除外した追加+削除行)は境界ゲート検出器と共通であり、migrations/ 配下は
tests/・docs/ のどちらにも該当しないため lines_code に含まれる。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# プロジェクトルートをパスに追加(scripts.gate_check の import 用)
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.gate_check import (  # noqa: E402
    DEPENDENCY_LOCK_FILE,
    MAX_FILES,
    MAX_LINES,
    NumstatRow,
    count_diff_size,
    get_head_sha,
    get_merge_base,
    parse_numstat,
)

PR_SIZE_COMMENT_MARKER = "<!-- ccm-pr-size -->"

# ok/large の境界は gate_check の MAX_LINES/MAX_FILES を共用する。large/oversized の
# 境界(800行)は本スクリプト独自の付加価値分類であり、gate_check には存在しない。
LARGE_LINES_MAX = 800

_REVERT_HEADING_RE = re.compile(r"^##\s*Revert\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)


# ---------------------------------------------------------------------------
# 計測
# ---------------------------------------------------------------------------


def _diff_numstat(repo: Path, merge_base: str, head_ref: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--numstat", "-M", "-z", merge_base, head_ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def _bucket(path: str) -> str:
    """行内訳の分類(migrations/ は code に含める。gate_check.count_diff_size と一致させる)。"""
    if path.startswith("tests/"):
        return "test"
    if path.endswith(".md") or path.startswith("docs/"):
        return "docs"
    return "code"


def _sum_lines(rows: list[NumstatRow]) -> int:
    return sum(r.additions + r.deletions for r in rows if not r.is_binary)


def _row_paths(row: NumstatRow) -> list[str]:
    """rename 検出(-M)時の旧パスも含めた、この行が触れる全パス。

    tests_touched / migrations_touched の判定を新パスだけで行うと、対象
    ディレクトリ配下から外へリネームされた変更を見落とす。gate_check の
    detect_migration_touch と同じく旧パスも突き合わせる。
    """
    return [row.path, row.old_path] if row.old_path else [row.path]


def classify_verdict(lines_code: int, files: int) -> str:
    if lines_code <= MAX_LINES and files <= MAX_FILES:
        return "ok"
    if lines_code <= LARGE_LINES_MAX:
        return "large"
    return "oversized"


def build_verdict(repo: Path, base_ref: str, head_ref: str) -> dict:
    merge_base = get_merge_base(repo, base_ref, head_ref)
    head_sha = get_head_sha(repo, head_ref)
    numstat_raw = _diff_numstat(repo, merge_base, head_ref)
    all_rows = parse_numstat(numstat_raw)

    lines_code, files = count_diff_size(all_rows)

    rows = [r for r in all_rows if r.path != DEPENDENCY_LOCK_FILE]
    lines_test = _sum_lines([r for r in rows if _bucket(r.path) == "test"])
    lines_docs = _sum_lines([r for r in rows if _bucket(r.path) == "docs"])
    lines_total = lines_code + lines_test + lines_docs

    tests_touched = any(_bucket(p) == "test" for r in rows for p in _row_paths(r))
    migrations_touched = any(p.startswith("migrations/") for r in rows for p in _row_paths(r))

    return {
        "schema_version": 1,
        "verdict": classify_verdict(lines_code, files),
        "lines_total": lines_total,
        "lines_code": lines_code,
        "lines_test": lines_test,
        "lines_docs": lines_docs,
        "files": files,
        "tests_touched": tests_touched,
        "migrations_touched": migrations_touched,
        "base_ref": base_ref,
        "merge_base": merge_base,
        "head": head_sha,
    }


# ---------------------------------------------------------------------------
# Revert 自己申告の突き合わせ
# ---------------------------------------------------------------------------


def _extract_revert_section(pr_body: str) -> str:
    match = _REVERT_HEADING_RE.search(pr_body)
    if not match:
        return ""
    start = match.end()
    next_heading = _NEXT_HEADING_RE.search(pr_body, start)
    end = next_heading.start() if next_heading else len(pr_body)
    return pr_body[start:end]


def _extract_revert_declaration(pr_body: str) -> Optional[str]:
    """PR body の `## Revert` セクションから R1/R2 の自己申告を読み取る。

    テンプレのガイドコメントをそのまま残した(R1/R2 両方が文中に出る)場合や
    どちらも書かれていない場合は None を返し、突き合わせを行わない。
    """
    section = _extract_revert_section(pr_body)
    has_r1 = re.search(r"\bR1\b", section) is not None
    has_r2 = re.search(r"\bR2\b", section) is not None
    if has_r1 and not has_r2:
        return "R1"
    if has_r2 and not has_r1:
        return "R2"
    return None


def check_revert_mismatch(pr_body: str, migrations_touched: bool) -> Optional[str]:
    declared = _extract_revert_declaration(pr_body)
    if declared == "R1" and migrations_touched:
        return (
            "Revert 分類が R1(migration 非接触)と申告されていますが、"
            "migrations_touched=true です。R2 への修正、または migrations/ 配下の変更が"
            "意図通りか確認してください。"
        )
    return None


# ---------------------------------------------------------------------------
# 出力レンダリング
# ---------------------------------------------------------------------------


def render_text(verdict: dict) -> str:
    lines = [
        f"verdict: {verdict['verdict']}",
        f"lines: code={verdict['lines_code']} test={verdict['lines_test']} "
        f"docs={verdict['lines_docs']} total={verdict['lines_total']}"
        f"  (ok<={MAX_LINES} / large<={LARGE_LINES_MAX})",
        f"files: {verdict['files']}  (ok<={MAX_FILES})",
        f"tests_touched={verdict['tests_touched']}  migrations_touched={verdict['migrations_touched']}",
    ]
    return "\n".join(lines) + "\n"


def render_comment_body(verdict: dict, revert_note: Optional[str]) -> str:
    lines = [PR_SIZE_COMMENT_MARKER, "## PR サイズ検査", "", f"**判定: {verdict['verdict']}**", ""]
    lines.append("| 項目 | 値 |")
    lines.append("|---|---|")
    lines.append(f"| 本体コード行 | {verdict['lines_code']} (ok ≤ {MAX_LINES}, large ≤ {LARGE_LINES_MAX}) |")
    lines.append(f"| テスト行 | {verdict['lines_test']} |")
    lines.append(f"| ドキュメント行 | {verdict['lines_docs']} |")
    lines.append(f"| 合計行 | {verdict['lines_total']} |")
    lines.append(f"| ファイル数 | {verdict['files']} (ok ≤ {MAX_FILES}) |")
    lines.append(f"| テスト変更 | {'あり' if verdict['tests_touched'] else 'なし'} |")
    lines.append(f"| migration 変更 | {'あり' if verdict['migrations_touched'] else 'なし'} |")
    lines.append("")
    if revert_note:
        lines.append(f"> ⚠️ {revert_note}")
        lines.append("")
    lines.append("<details><summary>JSON</summary>")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(verdict, ensure_ascii=False, sort_keys=True, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GitHub Actions 連携
# ---------------------------------------------------------------------------


def _load_pr_context() -> tuple[str, int, str, list[str], str]:
    """GITHUB_EVENT_PATH / GITHUB_REPOSITORY から (repo, pr_number, pr_body, labels, base_ref) を読む。"""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not event_path or not repo:
        raise RuntimeError(
            "GITHUB_EVENT_PATH/GITHUB_REPOSITORY が未設定です(--ci は GitHub Actions 上でのみ動作します)"
        )
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    pr_number = pr.get("number")
    if pr_number is None:
        raise RuntimeError("イベントに pull_request.number がありません")
    pr_body = pr.get("body") or ""
    labels = [label.get("name", "") for label in (pr.get("labels") or [])]
    base_ref = (pr.get("base") or {}).get("ref") or "main"
    return repo, int(pr_number), pr_body, labels, base_ref


def _find_existing_comment(repo: str, pr_number: int) -> Optional[int]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    comments = json.loads(result.stdout.decode("utf-8"))
    for comment in comments:
        if isinstance(comment, dict) and PR_SIZE_COMMENT_MARKER in (comment.get("body") or ""):
            return comment["id"]
    return None


def upsert_pr_comment(repo: str, pr_number: int, body: str) -> None:
    """マーカー付きコメントを 1 件に保つ(既存があれば更新、なければ新規作成)。"""
    existing_id = _find_existing_comment(repo, pr_number)

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        body_path = f.name
    try:
        if existing_id is not None:
            endpoint = f"repos/{repo}/issues/comments/{existing_id}"
            method = "PATCH"
        else:
            endpoint = f"repos/{repo}/issues/{pr_number}/comments"
            method = "POST"
        subprocess.run(
            ["gh", "api", "--method", method, endpoint, "-F", f"body=@{body_path}"],
            check=True,
        )
    finally:
        Path(body_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PR サイズ検査: 本体コード行/ファイル数を計測し ok/large/oversized を判定する")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true", help="ローカル自己チェック(人間向け出力。--json で機械可読)")
    mode.add_argument("--ci", action="store_true", help="GitHub Actions 上で実行し、PR コメントを upsert する")
    parser.add_argument("--base", help="比較元 ref(既定: origin/main。--ci では PR の base ブランチを自動使用)")
    parser.add_argument("--head", default="HEAD", help="比較先 ref(既定: HEAD)")
    parser.add_argument("--repo", default=".", help="git リポジトリのパス(既定: カレントディレクトリ)")
    parser.add_argument("--json", action="store_true", help="--local で JSON 出力する(既定は人間向けテキスト)")
    return parser


def run_local(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    base = args.base or "origin/main"
    verdict = build_verdict(repo, base, args.head)
    if args.json:
        sys.stdout.write(json.dumps(verdict, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(verdict))
    return 0


def run_ci(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    repo_slug, pr_number, pr_body, labels, base_ref = _load_pr_context()
    base = args.base or f"origin/{base_ref}"
    verdict = build_verdict(repo, base, args.head)

    revert_note = check_revert_mismatch(pr_body, verdict["migrations_touched"])
    body = render_comment_body(verdict, revert_note)
    # コメント投稿の失敗(fork PR の read-only トークン・API 障害等)は判定結果と独立。
    # ここで例外を握り潰さないと PR_SIZE_ENFORCE の値に関わらずジョブが落ち、
    # warn モードの「コメントのみで知らせ、ジョブは失敗させない」意図が壊れる。
    try:
        upsert_pr_comment(repo_slug, pr_number, body)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"warning: PR サイズ検査コメントの投稿に失敗しました(ジョブは継続します): {exc}\n")

    sys.stdout.write(json.dumps(verdict, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    enforce = os.environ.get("PR_SIZE_ENFORCE", "warn")
    is_exempt = "size-exempt" in labels
    if enforce == "fail" and verdict["verdict"] == "oversized" and not is_exempt:
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.ci:
        return run_ci(args)
    return run_local(args)


if __name__ == "__main__":
    sys.exit(main())
