#!/usr/bin/env python3
"""破壊的変更 migration lint: DROP/DELETE/UPDATE 等の破壊的 SQL を検出し、
`-- destructive: <理由>` 宣言が無いファイルを失敗させる。

標準ライブラリのみで動き、`uv run python scripts/migration_lint.py` として直接
実行できるほか、`lint_file()` / `lint_files()` を他モジュールから import して
再利用できる(migration 安全化パイプラインの dry-run ゲートが再利用する想定)。

DDL・破壊 SQL の正規表現パターン定数は `scripts/gate_check.py` を単一ソースとし、
本モジュールはそれを import して使う。本モジュール側の付加価値はパターンそのもの
ではなく、SQL 文分割・ルール分類(severity)・`-- destructive:` 宣言制である。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.gate_check import DDL_PATTERNS, DESTRUCTIVE_SQL_PATTERNS  # noqa: E402

# ---------------------------------------------------------------------------
# 既存 migration の grandfathering
#
# migrations/ には本 lint 導入時点(migration 番号 0048 が最終)で 52 ファイルが
# 存在し、そのうち複数が DROP TABLE / DROP COLUMN / DELETE FROM を含む(テーブル
# 再構築・データ移行の実績)。適用済み migration ファイルの事後改変は禁止のため
# `-- destructive:` ヘッダを後付けできない。番号がこの基準値以下のファイルは
# 破壊的ルールの宣言義務を免除する(missing-depends は全既存ファイルが元々ヘッダを
# 持つため対象外、duplicate-number は既存の重複 4 組を対象外にする)。
# ---------------------------------------------------------------------------

GRANDFATHER_MAX_NUMBER = 48

_DESTRUCTIVE_RULES = frozenset({"drop-table", "drop-column", "delete-from", "update-without-where"})
_DESTRUCTIVE_RULE_NAMES = _DESTRUCTIVE_RULES | {"table-rebuild"}


@dataclass
class Finding:
    rule: str  # "drop-table" | "drop-column" | "delete-from" |
    # "update-without-where" | "table-rebuild" | "missing-depends" | "duplicate-number"
    severity: str  # "error" | "warn" | "info"
    line: int
    message: str


@dataclass
class LintResult:
    path: str
    findings: list[Finding] = field(default_factory=list)
    destructive_declared: bool = False  # ヘッダに "-- destructive:" があるか
    is_destructive: bool = False  # error/warn 級の破壊的ルールに 1 件以上ヒットしたか


# ---------------------------------------------------------------------------
# コメント除去・文分割(BEGIN/CASE ... END を深さとして扱い、深さ0の ';' でのみ分割)
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    """`--` 行コメントと `/* */` ブロックコメントを空白に置換する(文字列リテラルは保持)。

    改行位置・全体の文字インデックスは変更しない(行番号計算・スライスに元テキストの
    インデックスをそのまま使えるようにするため)。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_squote = False
    while i < n:
        ch = text[i]
        if in_squote:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append(text[i + 1])
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_STATEMENT_TOKEN_RE = re.compile(r"'(?:[^']|'')*'" r"|\bBEGIN\b" r"|\bCASE\b" r"|\bEND\b" r"|;", re.IGNORECASE)


@dataclass(frozen=True)
class SqlStatement:
    start_line: int
    text: str  # 元テキスト(表示・evidence用)
    stripped_text: str  # コメント除去後(ルール判定用。コメント文中の SQL キーワードで誤検知しない)


def split_statements(text: str) -> list[SqlStatement]:
    """コメント除去 + 文字列リテラル対応の `;` 分割で文リストを返す。

    `BEGIN ... END`(トリガ本体)・`CASE ... END` はネストとして深さを積み、
    深さ 0 の `;` でのみ文を区切る。これによりトリガ本体内の `;` は分割対象に
    ならず、CREATE TRIGGER 文全体が 1 文として扱われる。
    """
    stripped = _strip_comments(text)
    stack: list[str] = []
    boundaries: list[int] = []
    for m in _STATEMENT_TOKEN_RE.finditer(stripped):
        tok = m.group(0)
        if tok == ";":
            if not stack:
                boundaries.append(m.start())
            continue
        upper = tok.upper()
        if upper in ("BEGIN", "CASE"):
            stack.append(upper)
        elif upper == "END":
            if stack:
                stack.pop()
        # それ以外(引用文字列)は読み飛ばす

    statements: list[SqlStatement] = []
    start = 0
    for b in boundaries:
        segment = text[start:b]
        if segment.strip():
            start_line = text.count("\n", 0, start) + 1
            statements.append(SqlStatement(start_line=start_line, text=segment, stripped_text=stripped[start:b]))
        start = b + 1
    tail = text[start:]
    if tail.strip():
        start_line = text.count("\n", 0, start) + 1
        statements.append(SqlStatement(start_line=start_line, text=tail, stripped_text=stripped[start:]))
    return statements


# ---------------------------------------------------------------------------
# ルール検出(パターンは gate_check.py の定数を再利用)
# ---------------------------------------------------------------------------

_DDL_RE = [re.compile(p) for p in DDL_PATTERNS]
_DELETE_FROM_RE = re.compile(DESTRUCTIVE_SQL_PATTERNS[0])  # DELETE FROM
_UPDATE_SET_RE = re.compile(DESTRUCTIVE_SQL_PATTERNS[1])  # UPDATE <table> SET

_DROP_TABLE_RE = re.compile(r"(?i)\bDROP\s+TABLE\b")
_DROP_COLUMN_RE = re.compile(r"(?i)\bDROP\s+COLUMN\b")
_WHERE_RE = re.compile(r"(?i)\bWHERE\b")
_SQLITE_SEQUENCE_DELETE_RE = re.compile(r"(?i)\bDELETE\s+FROM\s+sqlite_sequence\b")
_CREATE_TRIGGER_RE = re.compile(r"(?i)^\s*CREATE\s+TRIGGER\b")
_INSERT_SELECT_RE = re.compile(r"(?i)\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b.*?\bSELECT\b", re.S)
_RENAME_TABLE_RE = re.compile(r"(?i)\bALTER\s+TABLE\s+\S+\s+RENAME\s+TO\b")

_DEPENDS_HEADER_RE = re.compile(r"(?im)^\s*--\s*depends:")
_DESTRUCTIVE_HEADER_RE = re.compile(r"(?im)^\s*--\s*destructive:\s*(\S.*)$")
_LINT_OK_RE = re.compile(r"(?im)^\s*--\s*lint-ok:\s*([A-Za-z0-9_-]+)\s+(\S.*)$")
_NUMBER_PREFIX_RE = re.compile(r"^(\d+)_")


def _has_ddl(text: str) -> bool:
    return any(p.search(text) for p in _DDL_RE)


def _find_rebuild_pattern(statements: list[SqlStatement]) -> bool:
    joined = "\n".join(s.stripped_text for s in statements)
    return bool(_INSERT_SELECT_RE.search(joined)) and bool(_RENAME_TABLE_RE.search(joined))


def _detect_statement_findings(statements: list[SqlStatement]) -> list[Finding]:
    findings: list[Finding] = []
    has_rebuild = _find_rebuild_pattern(statements)

    for stmt in statements:
        text = stmt.stripped_text
        is_trigger_body = bool(_CREATE_TRIGGER_RE.match(text))

        if _DROP_TABLE_RE.search(text) and _has_ddl(text):
            if has_rebuild:
                findings.append(
                    Finding(
                        rule="table-rebuild",
                        severity="warn",
                        line=stmt.start_line,
                        message="テーブル再構築パターン(CREATE + INSERT SELECT + DROP/RENAME)として検出。DROP TABLE 単体の error からは降格",
                    )
                )
            else:
                findings.append(
                    Finding(
                        rule="drop-table",
                        severity="error",
                        line=stmt.start_line,
                        message="DROP TABLE を検出",
                    )
                )

        if _DROP_COLUMN_RE.search(text) and _has_ddl(text):
            findings.append(
                Finding(
                    rule="drop-column",
                    severity="error",
                    line=stmt.start_line,
                    message="ALTER TABLE ... DROP COLUMN を検出",
                )
            )

        if is_trigger_body:
            # トリガ本体内の DML はトリガ発火時にのみ実行されるものであり、
            # migration 適用時点でデータを破壊しない。delete-from /
            # update-without-where の対象外とする。
            continue

        if _DELETE_FROM_RE.search(text) and not _SQLITE_SEQUENCE_DELETE_RE.search(text):
            findings.append(
                Finding(
                    rule="delete-from",
                    severity="error",
                    line=stmt.start_line,
                    message="DELETE FROM を検出",
                )
            )

        if _UPDATE_SET_RE.search(text) and not _WHERE_RE.search(text):
            findings.append(
                Finding(
                    rule="update-without-where",
                    severity="error",
                    line=stmt.start_line,
                    message="WHERE 句の無い UPDATE を検出",
                )
            )

    return findings


def _migration_number(path: Path) -> Optional[int]:
    m = _NUMBER_PREFIX_RE.match(path.stem)
    return int(m.group(1)) if m else None


def _is_grandfathered(number: Optional[int]) -> bool:
    return number is not None and number <= GRANDFATHER_MAX_NUMBER


def _detect_duplicate_number(path: Path) -> list[Finding]:
    number = _migration_number(path)
    if _is_grandfathered(number):
        return []
    if number is None:
        return []
    siblings = [p for p in path.parent.glob("*.sql") if p != path]
    if any(_migration_number(p) == number for p in siblings):
        return [
            Finding(
                rule="duplicate-number",
                severity="warn",
                line=1,
                message=f"migration 番号 {number} が他ファイルと重複",
            )
        ]
    return []


def _detect_missing_depends(text: str) -> list[Finding]:
    if _DEPENDS_HEADER_RE.search(text):
        return []
    return [
        Finding(
            rule="missing-depends",
            severity="error",
            line=1,
            message="`-- depends:` ヘッダが見つからない",
        )
    ]


# ---------------------------------------------------------------------------
# ファイル単位の lint
# ---------------------------------------------------------------------------


def lint_file(path: "str | Path") -> LintResult:
    """1 ファイルを lint する。findings は `-- destructive:` 宣言・grandfather・
    `-- lint-ok:` により該当ルールが免除された場合 severity="info" に降格される。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")

    statements = split_statements(text)
    findings = _detect_statement_findings(statements)
    findings.extend(_detect_duplicate_number(p))
    findings.extend(_detect_missing_depends(text))

    is_destructive = any(f.rule in _DESTRUCTIVE_RULE_NAMES for f in findings)

    destructive_match = _DESTRUCTIVE_HEADER_RE.search(text)
    destructive_declared = destructive_match is not None

    lint_ok_rules = {m.group(1) for m in _LINT_OK_RE.finditer(text)}

    number = _migration_number(p)
    grandfathered = _is_grandfathered(number)

    exempt = destructive_declared or grandfathered

    downgraded: list[Finding] = []
    for f in findings:
        if f.severity == "error" and f.rule in _DESTRUCTIVE_RULES and (exempt or f.rule in lint_ok_rules):
            downgraded.append(Finding(rule=f.rule, severity="info", line=f.line, message=f.message))
        else:
            downgraded.append(f)

    return LintResult(
        path=str(p),
        findings=downgraded,
        destructive_declared=destructive_declared,
        is_destructive=is_destructive,
    )


def lint_files(paths: list[Path]) -> list[LintResult]:
    return [lint_file(p) for p in paths]


def lint_ok(result: LintResult) -> bool:
    """このファイルが lint を通るか(error 級の finding が残っていないか)。"""
    return not any(f.severity == "error" for f in result.findings)


# ---------------------------------------------------------------------------
# git 差分取得(--changed 用)
# ---------------------------------------------------------------------------


def _run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.decode("utf-8")


def get_changed_migration_files(repo: Path, base_ref: str = "origin/main", head_ref: str = "HEAD") -> list[Path]:
    """base_ref との merge-base から head_ref までの差分で、新規追加(status=A)
    された migrations/ 配下のファイルパスを返す。
    """
    merge_base = _run_git(repo, ["merge-base", base_ref, head_ref]).strip()
    raw = _run_git(repo, ["diff", "--name-status", "-M", merge_base, head_ref, "--", "migrations/"])
    paths: list[Path] = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status, file_path = parts[0], parts[-1]
        if status.startswith("A"):
            paths.append(repo / file_path)
    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _severity_marker(severity: str) -> str:
    return {"error": "ERROR", "warn": "WARN", "info": "info"}.get(severity, severity)


def _print_result(result: LintResult) -> None:
    if not result.findings:
        return
    print(f"{result.path}")
    for f in result.findings:
        print(f"  [{_severity_marker(f.severity)}] {f.rule} (line {f.line}): {f.message}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="migration の破壊的変更 lint")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="migrations/ 配下の全ファイルを lint する")
    group.add_argument("--changed", action="store_true", help="origin/main との差分(新規追加分)のみ lint する")
    parser.add_argument("--repo", default=".", help="git リポジトリのパス(既定: カレントディレクトリ)")
    parser.add_argument("--base", default="origin/main", help="--changed の比較元 ref")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    if args.all:
        migration_paths = sorted((repo / "migrations").glob("*.sql"))
    else:
        try:
            migration_paths = get_changed_migration_files(repo, base_ref=args.base)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(f"git 差分取得に失敗しました: {exc}\n")
            return 1

    if not migration_paths:
        print("対象ファイルなし")
        return 0

    results = lint_files(migration_paths)

    ok = True
    for result in results:
        _print_result(result)
        if not lint_ok(result):
            ok = False

    total_files = len(results)
    error_files = sum(1 for r in results if not lint_ok(r))
    print(f"\n{total_files} ファイル中 {error_files} ファイルで error 級の finding")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
